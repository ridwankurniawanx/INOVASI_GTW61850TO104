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

import libiec61850client
import libiec60870server
import lib60870

hosts_info = {}
async_msg = []
async_rpt = {}
INTERVAL = 0.1

# ==========================================================
# --- SAKLAR PENGATURAN MODE ---
# Atur ke True untuk mengaktifkan mode polling aktif.
# Atur ke False untuk menggunakan mode report-based (standar).
ENABLE_POLLING = True

# Interval dalam detik untuk mode polling (hanya digunakan jika ENABLE_POLLING = True)
POLLING_INTERVAL = 5
# ==========================================================


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


def readvaluecallback(key, data):
    """
    Versi callback yang sudah diperbaiki untuk mencocokkan path dengan benar.
    """
    global iec104_server
    logger.debug(f"callback: {key} - {data}")

    if not isinstance(data, dict) or 'value' not in data:
        logger.warning(f"Invalid data received for key '{key}': {data}")
        return

    # Parse 'key' yang dilaporkan untuk mendapatkan path-nya saja
    try:
        parsed_key_uri = urlparse(key)
        reported_path = parsed_key_uri.path.lstrip('/')
    except Exception:
        # Jika parsing gagal, gunakan key asli sebagai fallback
        reported_path = key

    value_to_update = data['value']
    final_value = find_first_float(value_to_update)

    if final_value is None:
        logger.warning(f"Could not extract a numeric value for key '{key}'. Structure: {value_to_update}")
        return

    found_match = False
    # Sekarang kita membandingkan path dengan path
    for config_path, ioa in mms_to_ioa_map.items():
        if config_path == reported_path: # Gunakan '==' untuk pencocokan persis
            try:
                numeric_value = float(final_value)
                iec104_server.update_ioa(ioa, numeric_value)
                logger.info(f"Successfully matched '{key}' to '{config_path}' and updated IOA {ioa} with value {numeric_value}")
                found_match = True
                break # Keluar dari loop setelah menemukan kecocokan
            except (ValueError, TypeError) as e:
                logger.warning(f"Value '{final_value}' could not be converted to float for IOA {ioa}. Error: {e}")

    if not found_match:
        # Pesan warning ini sekarang jauh lebih andal
        logger.warning(f"Could not find any matching config for reported path: {reported_path}")


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

    logger.info("Gateway started")
    if ENABLE_POLLING:
        logger.info(f"Mode Polling diaktifkan. Interval: {POLLING_INTERVAL} detik.")
    else:
        logger.info("Mode Report-Based diaktifkan.")

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
                        logger.warning(f"Duplicate MMS path '{mms_path}' found. The former will be used.")
                    else:
                        mms_to_ioa_map[mms_path] = int(ioa)
                except Exception as e:
                    logger.error(f"Error processing config line for IOA {ioa}: {mms_uri} - {e}")

    logger.info("Reverse mapping built.")
    logger.debug(f"Map content: {mms_to_ioa_map}")

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
                register_datapoint(config[section][item])

    register_datapoint_finished()

    for section, mms_type in command_types.items():
        if section in config:
            for item in config[section]:
                if iec104_server.add_ioa(int(item), mms_type, 0, command_60870_callback, False) == 0:
                    logger.info(f"{mms_type.__name__} registered for IOA {item}")
                else:
                    logger.error(f"Duplicate IOA: {item}, IOA not added to list")
    
    try:
        while True:
            if ENABLE_POLLING:
                # Mode polling aktif
                logger.debug("--- Triggering polling cycle ---")
                
                # Tahap 1: Loop dan picu pembacaan untuk semua data point
                for section in data_types:
                    if section in config:
                        for ioa_str, mms_path in config[section].items():
                            # Kita hanya panggil read_value untuk memicu, abaikan return value-nya
                            read_value(mms_path)
                
                # Tahap 2: Panggil client.poll() untuk memproses jawaban/callback yang masuk
                # Ini akan memicu readvaluecallback untuk data yang berhasil dibaca
                client.poll()

                logger.debug("--- Polling cycle finished, sleeping ---")
                time.sleep(POLLING_INTERVAL)
            else:
                # Mode report-based (default)
                time.sleep(INTERVAL)
                client.poll()
                logger.debug("Values polled, waiting for reports...")
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
