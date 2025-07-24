#!/usr/bin/env python3

import socket
import json
import subprocess
import time
import sys
import os
import logging

import libiec61850client
import libiec60870server
import lib60870
import configparser

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


# callbacks from libiec61850client
# called by client.poll
def readvaluecallback(key, data):
    global iec104_server
    global config
    logger.debug(f"callback: {key} - {data}")

    if not isinstance(data, dict) or 'value' not in data:
        logger.warning(f"Invalid data received for key '{key}': {data}")
        return

    value_to_update = data['value']

    # Jika nilainya adalah list (dari struktur), cari float pertama di dalamnya
    if isinstance(value_to_update, list):
        final_value = find_first_float(value_to_update)
        if final_value is not None:
            logger.debug(f"Extracted value {final_value} from nested list for key {key}")
            value_to_update = final_value
        else:
            logger.warning(f"Could not find any float in the nested list for key '{key}'. Structure: {value_to_update}")
            return

    for item_type in config:
        for ioa in config[item_type]:
            if config[item_type][ioa] == key:
                try:
                    numeric_value = float(value_to_update)
                    iec104_server.update_ioa(int(ioa), numeric_value)
                    logger.debug(f"Successfully updated IOA {ioa} with value {numeric_value}")
                except (ValueError, TypeError):
                    logger.warning(f"Final value '{value_to_update}' is not a valid number for key '{key}'. Skipping update.")
                return

    logger.warning(f"Could not find IOA for key: {key}")


# callback commandtermination
def cmdTerm_cb(msg):
    async_msg.append(msg)

# callback report
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
    client = libiec61850client.iec61850client(readvaluecallback, logger, cmdTerm_cb, Rpt_cb)
    iec104_server = libiec60870server.IEC60870_5_104_server()
    iec104_server.start()

    data_types = {
        'measuredvaluescaled': lib60870.MeasuredValueScaled,
        'singlepointinformation': lib60870.SinglePointInformation,
        'doublepointinformation': lib60870.DoublePointInformation,
    }

    command_types = {
        'singlepointcommand': lib60870.SingleCommand,
        'doublepointcommand': lib60870.DoubleCommand,
    }

    # Register data points for monitoring
    for section, mms_type in data_types.items():
        if section in config:
            for item in config[section]:
                if iec104_server.add_ioa(int(item), mms_type, 0, read_60870_callback, True) == 0:
                    register_datapoint(config[section][item])
                else:
                    logger.error(f"Duplicate IOA: {item}, IOA not added to list")

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
