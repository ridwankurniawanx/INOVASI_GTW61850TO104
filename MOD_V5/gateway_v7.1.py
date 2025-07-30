#!/usr/bin/env python3
# gateway_v7.1.py - Correct Async Object Init & Source Logging
# Deskripsi: Gateway IEC 61850 ke IEC 60870-5-104.
# Fitur v7.1: Memperbaiki 'different loop' pada shutdown_event dan menambahkan logging pembeda.
# Rocky, debug from report atau polling

import asyncio
import json
import logging
import configparser
import threading
import sys
import os
import time
from urllib.parse import urlparse

import libiec61850client_cached as libiec61850client
import libiec60870server
from lib60870 import *
from lib61850 import IedConnection_getState

# --- Definisikan konstanta ---
CON_STATE_NOT_CONNECTED, CON_STATE_CONNECTING, CON_STATE_CONNECTED, CON_STATE_CLOSING, CON_STATE_CLOSED = 0, 1, 2, 3, 4
POLLING_INTERVAL = 5
RECONNECT_DELAY = 15
POLL_REQUEST_DELAY = 0.05

# --- Variabel & Objek Global ---
clients_dict_lock = threading.Lock()
ied_locks = {}
ied_clients = {}
ied_to_ioas_map, mms_to_ioa_map, ioa_inversion_map, ioa_to_mms_config = {}, {}, {}, {}
update_queue = None
shutdown_event = None # ### PERBAIKAN 1: Inisialisasi sebagai None
iec104_server = None
main_loop = None

# --- Fungsi-fungsi utilitas & callback sinkronus (TIDAK ADA PERUBAHAN) ---
# ... (Semua fungsi dari find_first_float hingga invalidate_ied_points sama persis) ...
def find_first_float(data):
    if isinstance(data, float): return data
    if isinstance(data, int): return float(data)
    if isinstance(data, list):
        for item in data:
            result = find_first_float(item)
            if result is not None: return result
    return None

def command_60870_callback(ioa, ioa_data, srv, select_value):
    config_line = ioa_to_mms_config.get(ioa)
    if not config_line: return -1
    try:
        parsed_uri = urlparse(config_line)
        ied_id = f"{parsed_uri.hostname}:{parsed_uri.port or 102}"
    except Exception: return -1

    with clients_dict_lock:
        client = ied_clients.get(ied_id)

    if not client:
        logging.error(f"Command for {ied_id} failed: client not connected.")
        return -1

    ied_lock = ied_locks.get(ied_id)
    if not ied_lock:
        logging.error(f"Command for {ied_id} failed: lock not found.")
        return -1

    with ied_lock:
        val_str = "true" if ioa_data['data'] == 1 else "false"
        if select_value:
            return client.select(str(config_line), val_str)
        else:
            return client.operate(str(config_line), val_str)

def process_data_update(ied_id, key, data):
    if not isinstance(data, dict) or 'value' not in data: return
    reported_key, value_to_update = key, data['value']
    final_value = find_first_float(value_to_update)
    if final_value is None: return

    mms_path_from_key = reported_key
    if "iec61850://" in reported_key:
        try:
            parsed_uri = urlparse(reported_key)
            mms_path_from_key = parsed_uri.path.lstrip('/')
        except Exception:
            logging.warning(f"Could not parse URI key: {reported_key}")
            return

    valid_ioas_for_ied = set(ied_to_ioas_map.get(ied_id, []))
    if not valid_ioas_for_ied: return

    found_match = False
    for config_path, ioa in mms_to_ioa_map.items():
        if ioa in valid_ioas_for_ied and config_path.startswith(mms_path_from_key):
            try:
                ioa_type_class = iec104_server.IOA_list.get(ioa, {}).get('type')
                ioa_type = str(ioa_type_class)
                value_to_send = float(final_value)

                if "DoublePointInformation" in ioa_type:
                    val_map = {1.0: 1, 2.0: 2}; value_to_send = val_map.get(value_to_send, 0)
                elif "SinglePointInformation" in ioa_type:
                    value_to_send = 1 if int(value_to_send) != 0 else 0

                if ioa_inversion_map.get(ioa, False):
                    if value_to_send == 1: value_to_send = 2
                    elif value_to_send == 2: value_to_send = 1

                iec104_server.update_ioa(ioa, value_to_send)
                logging.info(f"[{ied_id}] Matched '{reported_key}' to IOA {ioa}, updated with: {value_to_send}")
                found_match = True
                break
            except Exception as e:
                logging.error(f"Error processing update for IOA {ioa}: {e}", exc_info=True)
            break

    if not found_match:
        logging.warning(f"[{ied_id}] No matching config for key: {reported_key}")

def do_invalidation(ied_id):
    if ied_id in ied_to_ioas_map:
        ioas_to_invalidate = ied_to_ioas_map[ied_id]
        logging.warning(f"Invalidating {len(ioas_to_invalidate)} data points for {ied_id}.")
        al_params = iec104_server.alParams
        quality_flags = 48 # IV (invalid) bit set
        for ioa in ioas_to_invalidate:
            ioa_config = iec104_server.IOA_list.get(ioa)
            if ioa_config:
                io_type = ioa_config.get('type')
                creator_func = None
                if io_type == MeasuredValueScaled: creator_func = MeasuredValueScaled_create
                elif io_type == MeasuredValueShort: creator_func = MeasuredValueShort_create
                elif io_type == SinglePointInformation: creator_func = SinglePointInformation_create
                elif io_type == DoublePointInformation: creator_func = DoublePointInformation_create
                if creator_func:
                    new_asdu = CS101_ASDU_create(al_params, False, CS101_COT_SPONTANEOUS, 0, 1, False, False)
                    io = cast(creator_func(None, ioa, 0, quality_flags), InformationObject)
                    CS101_ASDU_addInformationObject(new_asdu, io)
                    InformationObject_destroy(io)
                    CS104_Slave_enqueueASDU(iec104_server.slave, new_asdu)
                    CS101_ASDU_destroy(new_asdu)
        for ioa in ioas_to_invalidate:
            if ioa in iec104_server.IOA_list:
                iec104_server.IOA_list[ioa]['data'] = float('nan')

def ied_data_callback(key, data, ied_id):
    if main_loop and update_queue:
        update_item = {'type': 'data', 'ied_id': ied_id, 'key': key, 'data': data}
        main_loop.call_soon_threadsafe(update_queue.put_nowait, update_item)
    else:
        logging.warning(f"[{ied_id}] Main loop/queue not available, data point dropped.")

def invalidate_ied_points(ied_id):
    if main_loop and update_queue:
        update_item = {'type': 'invalidate', 'ied_id': ied_id}
        main_loop.call_soon_threadsafe(update_queue.put_nowait, update_item)
    else:
        logging.warning(f"[{ied_id}] Main loop/queue not available, invalidation dropped.")

# --- ASYNC TASKS ---

async def ied_handler(ied_id, uris):
    logging.info(f"[{ied_id}] IED handler task started.")
    loop = asyncio.get_running_loop()
    client = None

    ied_locks[ied_id] = threading.Lock()
    ied_lock = ied_locks[ied_id]

    # ### TAMBAHAN: Logging Pembeda ###
    def polling_entry_point(key, data):
        logging.debug(f"[{ied_id}] Data received via POLLING for key: {key}")
        ied_data_callback(key, data, ied_id)

    def report_entry_point(key, data):
        logging.debug(f"[{ied_id}] Data received via REPORT for key: {key}")
        ied_data_callback(key, data, ied_id)
    # ### AKHIR TAMBAHAN ###

    def locked_check_state():
        with ied_lock:
            if not client or not client.getRegisteredIEDs().get(ied_id, {}).get('con'):
                return False
            conn_info = client.getRegisteredIEDs()[ied_id]
            if conn_info.get('con'):
                state = IedConnection_getState(conn_info['con'])
                return state == CON_STATE_CONNECTED
            return False

    def locked_register_and_poll():
        with ied_lock:
            for uri in uris:
                client.registerReadValue(str(uri))
            logging.debug(f"[{ied_id}] Executing library's internal poll...")
            client.poll()

    while not (shutdown_event and shutdown_event.is_set()):
        try:
            logging.info(f"[{ied_id}] Attempting to connect...")
            with ied_lock:
                client = libiec61850client.iec61850client(
                    readvaluecallback=polling_entry_point,
                    loggerRef=logging,
                    cmdTerm_cb=None,
                    Rpt_cb=report_entry_point
                )

            res = await loop.run_in_executor(None, client.getIED, ied_id.split(':')[0], int(ied_id.split(':')[1]))
            if res != 0:
                raise ConnectionError("getIED failed, connection or discovery error.")

            with clients_dict_lock:
                ied_clients[ied_id] = client
            logging.info(f"[{ied_id}] Connection successful. Registering values...")

            await loop.run_in_executor(None, locked_register_and_poll)
            logging.info(f"[{ied_id}] Values registered. Now entering main loop (Report listening & Fallback polling).")

            while not (shutdown_event and shutdown_event.is_set()):
                is_connected = await loop.run_in_executor(None, locked_check_state)
                if not is_connected:
                    raise ConnectionError("Connection lost (proactive check).")

                await loop.run_in_executor(None, client.poll)
                logging.debug(f"[{ied_id}] Main loop waiting for {POLLING_INTERVAL}s.")
                await asyncio.sleep(POLLING_INTERVAL)

        except Exception as e:
            logging.error(f"[{ied_id}] Handler error: {e}. Reconnecting in {RECONNECT_DELAY}s.")
            with clients_dict_lock:
                if ied_id in ied_clients:
                    del ied_clients[ied_id]
            invalidate_ied_points(ied_id)
            try:
                if shutdown_event:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=RECONNECT_DELAY)
            except asyncio.TimeoutError:
                pass # Lanjutkan loop untuk mencoba reconnect

async def iec104_processor():
    logging.info("IEC 104 processor task started.")
    loop = asyncio.get_running_loop()
    while not (shutdown_event and shutdown_event.is_set()):
        try:
            update = await update_queue.get()
            if update['type'] == 'data':
                await loop.run_in_executor(None, process_data_update, update['ied_id'], update['key'], update['data'])
            elif update['type'] == 'invalidate':
                await loop.run_in_executor(None, do_invalidation, update['ied_id'])
            update_queue.task_done()
        except asyncio.CancelledError:
            logging.info("IEC 104 processor task cancelled.")
            break
        except Exception as e:
            logging.error(f"Error in IEC 104 processor: {e}", exc_info=True)

async def main():
    global iec104_server, main_loop, update_queue, shutdown_event

    # ### PERBAIKAN 2: Inisialisasi semua objek asyncio di sini ###
    main_loop = asyncio.get_running_loop()
    update_queue = asyncio.Queue()
    shutdown_event = asyncio.Event()

    logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s', level=logging.DEBUG)
    logger = logging.getLogger('gateway-async-hybrid-fixed')

    config = configparser.ConfigParser(); config.optionxform = str
    config_file = sys.argv[1] if len(sys.argv) > 1 else 'config.local.ini'
    if not os.path.exists(config_file): logger.error(f"Config file not found: {config_file}"); sys.exit(1)
    config.read(config_file)
    logger.info("Gateway v7.1 (Robust Async Init & Source Logging) started")

    iec104_server = libiec60870server.IEC60870_5_104_server()
    data_types = {'measuredvaluescaled': MeasuredValueScaled, 'measuredvaluefloat': MeasuredValueShort,
                  'singlepointinformation': SinglePointInformation, 'doublepointinformation': DoublePointInformation}
    command_types = {'singlepointcommand': SingleCommand, 'doublepointcommand': DoubleCommand}

    logger.info("Parsing configuration...")
    ied_data_groups = {}
    all_sections = list(data_types.keys()) + list(command_types.keys())
    for section in all_sections:
        if section in config:
            for ioa, config_line in config[section].items():
                uri_part, should_invert = config_line, False
                if config_line.endswith(':invers=true'): uri_part, _ = config_line.rsplit(':', 1); should_invert = True
                parsed = urlparse(uri_part)
                ied_id = f"{parsed.hostname}:{parsed.port or 102}"
                ioa_int = int(ioa)
                if ied_id not in ied_to_ioas_map: ied_to_ioas_map[ied_id] = []
                if ioa_int not in ied_to_ioas_map[ied_id]: ied_to_ioas_map[ied_id].append(ioa_int)
                if section in data_types:
                    mms_to_ioa_map[parsed.path.lstrip('/')] = ioa_int
                    if ied_id not in ied_data_groups: ied_data_groups[ied_id] = []
                    if uri_part not in ied_data_groups[ied_id]:
                        ied_data_groups[ied_id].append(uri_part)
                if should_invert: ioa_inversion_map[ioa_int] = True
                if section in command_types: ioa_to_mms_config[ioa_int] = uri_part

    logger.info(f"Found {len(ied_data_groups)} unique IEDs to monitor.")
    for section, mms_type in data_types.items():
        if section in config:
            for item in config[section]: iec104_server.add_ioa(int(item), mms_type, 0, None, True)
    for section, mms_type in command_types.items():
        if section in config:
            for item in config[section]: iec104_server.add_ioa(int(item), mms_type, 0, command_60870_callback, False)

    server_thread = threading.Thread(target=iec104_server.start, daemon=True)
    server_thread.start()
    logger.info("IEC 104 server started in a separate thread.")

    tasks = [ied_handler(ied_id, uris) for ied_id, uris in ied_data_groups.items()]
    tasks.append(iec104_processor())

    try:
        await asyncio.gather(*tasks)
    finally:
        if shutdown_event:
            shutdown_event.set()
        logging.info("Shutting down all tasks.")
        if iec104_server:
            iec104_server.stop()
        logging.info("Gateway stopped.")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Gateway shutting down due to KeyboardInterrupt.")
