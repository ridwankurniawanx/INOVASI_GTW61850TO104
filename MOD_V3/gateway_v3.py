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
    global iec104_server
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
                # Dapatkan tipe IOA dari server untuk pengecekan
                ioa_type = iec104_server.IOA_list.get(ioa, {}).get('type')

                numeric_value = float(final_value)
                value_to_send = numeric_value

                # =================== PERBAIKAN DI SINI ===================
                # Jika tipe datanya DoublePointInformation, lakukan pemetaan nilai eksplisit
                # untuk memastikan tidak ada nilai terbalik.
                if ioa_type == lib60870.DoublePointInformation:
                    if numeric_value == 2:      # Jika dari 61850 nilainya 2 (ON)
                        value_to_send = 1       # Maka untuk 104 nilainya tetap 2 (ON)
                    elif numeric_value == 1:    # Jika dari 61850 nilainya 1 (OFF)
                        value_to_send = 2       # Maka untuk 104 nilainya tetap 1 (OFF)
                    else:
                        value_to_send = 0       # Selain itu, anggap intermediate (0) atau invalid
                    logger.info(f"DoublePoint mapping for IOA {ioa}: {numeric_value} -> {value_to_send}")
                # =========================================================

                # Kirim nilai yang sudah dipastikan benar ke server 104
                iec104_server.update_ioa(ioa, value_to_send)

                logger.info(f"Successfully matched '{reported_key}' to '{config_path}' and updated IOA {ioa} with value {value_to_send}")
                found_match = True

            except (ValueError, TypeError) as e:
                logger.warning(f"Value '{final_value}' could not be converted to float for IOA {ioa}. Error: {e}")

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

    # --- MODIFIKASI: Gunakan kelas dari pustaka yang sudah di-cache ---
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

    # MEMBUAT PEMETAAN TERBALIK (REVERSE MAPPING) DENGAN PARSING URI
    logger.info("Building reverse mapping for MMS to IOA...")
    mms_to_ioa_map = {}
    all_sections = list(data_types.keys()) + list(command_types.keys())

    for section in all_sections:
        if section in config:
            for ioa, mms_uri in config[section].items():
                try:
                    parsed_uri = urlparse(mms_uri)
                    mms_path = parsed_uri.path.lstrip('/')

                    if not mms_path:
                        logger.warning(f"Could not parse path from URI: '{mms_uri}' for IOA {ioa}. Skipping.")
                        continue

                    if mms_path in mms_to_ioa_map:
                        logger.warning(f"Duplicate MMS path '{mms_path}' found in config. It's mapped to IOA {mms_to_ioa_map[mms_path]} and now IOA {ioa}. The former will be used.")
                    else:
                        mms_to_ioa_map[mms_path] = int(ioa)
                except Exception as e:
                    logger.error(f"Error processing config line for IOA {ioa}: {mms_uri} - {e}")

    logger.info("Reverse mapping built.")
    logger.debug(f"Map content: {mms_to_ioa_map}")

    # ==========================================================
    # PERBAIKAN RACE CONDITION
    # ==========================================================
    # Tahap 1: Daftarkan SEMUA IOA ke server 104 terlebih dahulu
    logger.info("Registering all IOAs to the 104 server...")
    for section, mms_type in data_types.items():
        if section in config:
            for item in config[section]:
                if iec104_server.add_ioa(int(item), mms_type, 0, read_60870_callback, True) != 0:
                    logger.error(f"Duplicate IOA: {item}, IOA not added to list")

    # Tahap 2: Setelah semua IOA terdaftar, baru daftarkan ke client 61850 (yang akan mengaktifkan report)
    logger.info("Registering data points to the 61850 client...")
    for section in data_types:
        if section in config:
            for item in config[section]:
                register_datapoint(config[section][item])
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
