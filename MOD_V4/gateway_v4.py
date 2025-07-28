#!/usr/bin/env python3

import socket
import json
import subprocess
import time
import sys
import os
import logging
import configparser
from urllib.parse import urlparse

# --- MODIFIKASI: Impor pustaka klien yang sudah di-cache ---
import libiec61850client_cached as libiec61850client
import libiec60870server
import lib60870

hosts_info = {}
async_msg = []
async_rpt = {}
INTERVAL = 0.1

def find_first_float(data):
    """
    Fungsi rekursif untuk mencari nilai float pertama dalam struktur list/nested list.
    """
    if isinstance(data, float):
        return data
    if isinstance(data, int):
        return float(data)
    if isinstance(data, list):
        for item in data:
            result = find_first_float(item)
            if result is not None:
                return result
    return None

def read_value(id):
    logger.debug(f"read value: {id}")
    return client.ReadValue(id)


def write_value(id, value):
    global client
    logger.debug(f"write value: {value}, element: {id}")
    retValue, msg = client.registerWriteValue(str(id), str(value))
    if retValue > 0:
        return retValue, libiec61850client.IedClientError(retValue).name
    if retValue == 0:
        return retValue, "no error"
    return retValue, "general error"


def operate(id, value):
    logger.debug(f"operate: {id} v: {value}")
    val_str = "true" if value == 1 else "false"
    return client.operate(str(id), val_str)


def select(id, value):
    logger.debug(f"select: {id}")
    val_str = "true" if value == 1 else "false"
    return client.select(str(id), val_str)


def cancel(id):
    logger.debug(f"cancel: {id}")
    return client.cancel(str(id))


def register_datapoint(id):
    global client
    logger.debug(f"register datapoint: {id}")
    client.registerReadValue(str(id))


def register_datapoint_finished():
    global client
    ieds = client.getRegisteredIEDs()


# FUNGSI CALLBACK YANG SUDAH DIOPTIMASI DAN FLEKSIBEL
def readvaluecallback(key, data):
    global iec104_server, ioa_inversion_map
    logger.debug(f"callback: {key} - {data}")

    if not isinstance(data, dict) or 'value' not in data:
        logger.warning(f"Invalid data received for key '{key}': {data}")
        return

    reported_key = key
    value_to_update = data['value']
    final_value = find_first_float(value_to_update)

    if final_value is None:
        logger.warning(f"Could not extract a numeric value for key '{reported_key}'. Structure: {value_to_update}")
        return

    found_match = False
    for config_path, ioa in mms_to_ioa_map.items():
        if config_path.startswith(reported_key):
            try:
                ioa_type = iec104_server.IOA_list.get(ioa, {}).get('type')
                numeric_value = float(final_value)
                value_to_send = numeric_value # Nilai default

                # Cek apakah inversi diperlukan untuk IOA ini
                should_invert = ioa_inversion_map.get(ioa, False)

                # =================== LOGIKA INVERSI BARU ===================
                if ioa_type == lib60870.DoublePointInformation:
                    # Standard mapping: 1->OFF, 2->ON
                    val_map = {1.0: 1, 2.0: 2}
                    dp_val = val_map.get(numeric_value, 0) # 0 for intermediate/invalid
                    
                    if should_invert:
                        if dp_val == 1: value_to_send = 2
                        elif dp_val == 2: value_to_send = 1
                        else: value_to_send = dp_val # biarkan 0 (intermediate)
                        logger.info(f"Inverting DoublePoint for IOA {ioa}: {dp_val} -> {value_to_send}")
                    else:
                        value_to_send = dp_val

                elif ioa_type == lib60870.SinglePointInformation:
                    # Standard mapping: 0->OFF, 1->ON
                    sp_val = 1 if int(numeric_value) != 0 else 0

                    if should_invert:
                        value_to_send = 1 - sp_val # Cara cepat untuk membalik 0 dan 1
                        logger.info(f"Inverting SinglePoint for IOA {ioa}: {sp_val} -> {value_to_send}")
                    else:
                        value_to_send = sp_val
                # =========================================================

                iec104_server.update_ioa(ioa, value_to_send)
                logger.info(f"Successfully matched '{reported_key}' to '{config_path}' and updated IOA {ioa} with value {value_to_send}")
                found_match = True

            except (ValueError, TypeError) as e:
                logger.warning(f"Value '{final_value}' could not be converted to float for IOA {ioa}. Error: {e}")
            
            # Hentikan loop jika sudah menemukan match yang pas
            break 

    if not found_match:
        logger.warning(f"Could not find any matching config for reported key: {reported_key}")


def cmdTerm_cb(msg):
    async_msg.append(msg)

def Rpt_cb(key, value):
    async_rpt[key] = value
    readvaluecallback(key, value)


def read_60870_callback(ioa, ioa_data, iec104server):
    global config
    logger.debug("read callback called from lib60870")
    for item_type in config:
        if str(ioa) in config[item_type]:
            return read_value(config[item_type][str(ioa)])
    return -1


def command_60870_callback(ioa, ioa_data, iec104server, select_value):
    logger.debug("operate callback called from lib60870")
    for item_type in config:
        if str(ioa) in config[item_type]:
            if select_value:
                return select(config[item_type][str(ioa)], ioa_data['data'])
            else:
                return operate(config[item_type][str(ioa)], ioa_data['data'])
    return -1


if __name__ == '__main__':
    logger = logging.getLogger('gateway')
    logging.basicConfig(format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                        level=logging.INFO)

    config = configparser.ConfigParser()
    config.optionxform = str

    config_file = sys.argv[1] if len(sys.argv) > 1 else 'config.local.ini'
    if not os.path.exists(config_file):
        logger.error(f"Config file not found: {config_file}")
        sys.exit(1)
    config.read(config_file)

    logger.info("Gateway v3 (dengan cache struktur) started")

    client = libiec61850client.iec61850client(readvaluecallback, logger, cmdTerm_cb, Rpt_cb)

    iec104_server = libiec60870server.IEC60870_5_104_server()
    iec104_server.start()

    data_types = {
        'measuredvaluescaled': lib60870.MeasuredValueScaled,
        'measuredvaluefloat': lib60870.MeasuredValueShort,
        'singlepointinformation': lib60870.SinglePointInformation,
        'doublepointinformation': lib60870.DoublePointInformation,
    }

    command_types = {
        'singlepointcommand': lib60870.SingleCommand,
        'doublepointcommand': lib60870.DoubleCommand,
    }

    # <<< MODIFIKASI FINAL: Parsing untuk format 'URI:invers=true' >>>
    logger.info("Building reverse mapping and inversion config...")
    mms_to_ioa_map = {}
    ioa_inversion_map = {}
    all_sections = list(data_types.keys()) + list(command_types.keys())
    
    for section in all_sections:
        if section in config:
            for ioa, config_line in config[section].items():
                try:
                    uri_part = config_line
                    should_invert = False
                    
                    marker = ':invers=true'
                    # Cek apakah baris diakhiri dengan penanda (tanpa spasi)
                    if config_line.endswith(marker):
                        # Potong penanda untuk mendapatkan URI yang bersih
                        uri_part = config_line[:-len(marker)]
                        should_invert = True

                    # Lanjutkan dengan URI yang sudah bersih
                    parsed_uri = urlparse(uri_part)
                    mms_path = parsed_uri.path.lstrip('/')

                    if not mms_path:
                        logger.warning(f"Could not parse path from URI: '{uri_part}' for IOA {ioa}. Skipping.")
                        continue

                    if mms_path in mms_to_ioa_map:
                        logger.warning(f"Duplicate MMS path '{mms_path}' found. Overwriting previous entry.")
                    
                    mms_to_ioa_map[mms_path] = int(ioa)
                    if should_invert:
                        ioa_inversion_map[int(ioa)] = True
                        logger.info(f"IOA {ioa} is configured for value inversion.")

                except Exception as e:
                    logger.error(f"Error processing config line for IOA {ioa}: {config_line} - {e}")

    logger.info("Reverse mapping and inversion config built.")
    logger.debug(f"MMS->IOA Map: {mms_to_ioa_map}")
    logger.debug(f"Inversion Map: {ioa_inversion_map}")
    # <<< AKHIR DARI BLOK MODIFIKASI >>>

    # ==========================================================
    # PERBAIKAN RACE CONDITION
    # ==========================================================
    logger.info("Registering all IOAs to the 104 server...")
    for section, mms_type in data_types.items():
        if section in config:
            for item in config[section]:
                if iec104_server.add_ioa(int(item), mms_type, 0, read_60870_callback, True) != 0:
                    logger.error(f"Duplicate IOA: {item}, IOA not added to list")

    logger.info("Registering data points to the 61850 client...")
    for section in data_types:
        if section in config:
            for item in config[section]:
                # Ambil URI bersih tanpa opsi inversi saat mendaftar
                uri_to_register = config[section][item]
                marker = ':invers=true'
                if uri_to_register.endswith(marker):
                    uri_to_register = uri_to_register[:-len(marker)]
                register_datapoint(uri_to_register)
    # ==========================================================

    register_datapoint_finished()

    # Register command points
    for section, mms_type in command_types.items():
        if section in config:
            for item in config[section]:
                if iec104_server.add_ioa(int(item), mms_type, 0, command_60870_callback, False) == 0:
                    logger.info(f"{mms_type.__name__} registered for IOA {item}")
                else:
                    logger.error(f"Duplicate IOA: {item}, IOA not added to list")

    try:
        while True:
            time.sleep(INTERVAL)
            client.poll()
            logger.debug("Values polled")

            for key in list(async_rpt):
                async_rpt.pop(key)
                logger.debug(f"{key} updated via report")

    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)
    finally:
        if 'iec104_server' in locals() and iec104_server:
            iec104_server.stop()
        logger.info("Gateway stopped.")
