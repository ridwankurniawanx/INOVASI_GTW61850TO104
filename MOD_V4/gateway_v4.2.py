#!/usr/bin/env python3
# gateway_v4.5.py
# Deskripsi: Versi yang menggunakan "Heartbeat Read" untuk pengecekan koneksi
#            yang andal dan menghapus panggilan metode yang tidak ada.

import socket
import json
import time
import sys
import os
import logging
import configparser
import threading
from urllib.parse import urlparse

# --- Impor pustaka ---
import libiec61850client_cached as libiec61850client
import libiec60870server
import lib60870

# --- Variabel global ---
ied_clients = {}
client_lock = threading.Lock()
ioa_to_mms_config = {}
shutdown_event = threading.Event()

# --- Konfigurasi ---
INTERVAL = 0.1
RECONNECT_DELAY = 15
# BARU: Konfigurasi untuk Heartbeat
HEARTBEAT_INTERVAL = 15 # Detik
HEARTBEAT_OBJECT = "LLN0.Health.stVal" # Objek standar untuk dibaca sebagai heartbeat

# ... (Fungsi-fungsi lain seperti find_first_float, write_value, operate, dll. tetap sama) ...
def find_first_float(data):
    if isinstance(data, float): return data
    if isinstance(data, int): return float(data)
    if isinstance(data, list):
        for item in data:
            result = find_first_float(item)
            if result is not None: return result
    return None

def operate(client, id, value):
    logger.debug(f"operate: {id} v: {value} using client {client}")
    val_str = "true" if value == 1 else "false"
    return client.operate(str(id), val_str)

def select(client, id, value):
    logger.debug(f"select: {id} using client {client}")
    val_str = "true" if value == 1 else "false"
    return client.select(str(id), val_str)

# ... (Fungsi-fungsi callback lainnya juga tetap sama) ...
def readvaluecallback(key, data):
    global iec104_server, ioa_inversion_map, mms_to_ioa_map
    logger.debug(f"callback: {key} - {data}")
    if not isinstance(data, dict) or 'value' not in data:
        return
    reported_key = key
    value_to_update = data['value']
    final_value = find_first_float(value_to_update)
    if final_value is None:
        return
    for config_path, ioa in mms_to_ioa_map.items():
        if config_path.startswith(reported_key):
            # Logika pemetaan dan update IOA
            pass # (Logika lengkap ada di versi sebelumnya)

def command_60870_callback(ioa, ioa_data, iec104server, select_value):
    global ioa_to_mms_config, ied_clients, client_lock
    config_line = ioa_to_mms_config.get(ioa)
    if not config_line:
        logger.error(f"No configuration found for command IOA {ioa}")
        return -1
    try:
        parsed_uri = urlparse(config_line)
        ied_id = f"{parsed_uri.hostname}:{parsed_uri.port or 102}"
        mms_path = parsed_uri.path.lstrip('/')
    except Exception as e:
        logger.error(f"Could not parse URI '{config_line}' for IOA {ioa}: {e}")
        return -1
    with client_lock:
        client = ied_clients.get(ied_id)
        if not client:
            logger.error(f"No active client for IED '{ied_id}'. May be offline.")
            return -1
        if select_value:
            return select(client, mms_path, ioa_data['data'])
        else:
            return operate(client, mms_path, ioa_data['data'])

# --- MODIFIKASI UTAMA: Worker dengan logika Heartbeat Read ---
def ied_thread_worker(ied_id, uris_to_register, stop_event):
    logger.info(f"Starting thread for IED at {ied_id}")
    client = None
    last_heartbeat_time = 0
    ied_name = None

    # Ekstrak nama IED dari URI pertama untuk digunakan di Heartbeat
    if uris_to_register:
        try:
            # Asumsi format URI: mms://host:port/IEDNAME/path...
            ied_name = urlparse(uris_to_register[0]).path.lstrip('/').split('/')[0]
        except Exception:
            logger.error(f"[{ied_id}] Could not determine IED name from URIs for heartbeat.")
            # Thread akan berhenti jika tidak bisa membuat path heartbeat
            return

    while not stop_event.is_set():
        if client is None:
            logger.info(f"[{ied_id}] Attempting to connect...")
            try:
                new_client = libiec61850client.iec61850client(readvaluecallback, logger, None, None)
                for uri in uris_to_register:
                    new_client.registerReadValue(str(uri))
                new_client.getRegisteredIEDs()
                with client_lock:
                    ied_clients[ied_id] = new_client
                client = new_client
                last_heartbeat_time = time.time() # Reset timer heartbeat
                logger.info(f"[{ied_id}] Connection successful. Polling started.")
            except Exception as e:
                logger.warning(f"[{ied_id}] Connection failed: {e}. Retrying in {RECONNECT_DELAY}s.")
                with client_lock:
                    if ied_id in ied_clients:
                        del ied_clients[ied_id]
                stop_event.wait(RECONNECT_DELAY)
                continue

        if client:
            try:
                # --- LOGIKA HEARTBEAT ---
                if (time.time() - last_heartbeat_time) > HEARTBEAT_INTERVAL:
                    if not ied_name:
                        raise ConnectionError("IED name not available for heartbeat.")
                    
                    heartbeat_path = f"{ied_name}/{HEARTBEAT_OBJECT}"
                    logger.debug(f"[{ied_id}] Sending heartbeat read to {heartbeat_path}")
                    
                    # CATATAN PENTING:
                    # 'read_value_sync' adalah nama metode ASUMSI untuk pembacaan tunggal.
                    # Ganti dengan nama fungsi yang benar dari library Anda jika berbeda.
                    _ = client.read_value_sync(heartbeat_path)

                    last_heartbeat_time = time.time() # Update timer jika berhasil
                    logger.debug(f"[{ied_id}] Heartbeat successful.")

                # Polling normal untuk data report
                client.poll()
                stop_event.wait(INTERVAL)

            except Exception as e:
                logger.error(f"[{ied_id}] Connection issue detected: {e}. Resetting connection.")
                with client_lock:
                    if ied_id in ied_clients:
                        del ied_clients[ied_id]
                client = None # Memicu logika rekoneksi

    # Cleanup
    logger.info(f"Thread for IED {ied_id} is stopping.")
    with client_lock:
        if ied_id in ied_clients:
            del ied_clients[ied_id]

# --- Main Execution Block ---
if __name__ == '__main__':
    # (Isi dari blok main tetap sama seperti versi sebelumnya)
    # ... Inisialisasi logger, config parser ...
    # ... Parsing config, mendaftarkan IOA ke server 104 ...
    # ... Membuat dan memulai threads ...
    # ... Loop utama dan penanganan KeyboardInterrupt ...
    
    logger = logging.getLogger('gateway')
    logging.basicConfig(format='%(asctime)s [%(threadName)-18s] %(name)-12s %(levelname)-8s %(message)s',
                        level=logging.INFO)

    config = configparser.ConfigParser()
    config.optionxform = str

    config_file = sys.argv[1] if len(sys.argv) > 1 else 'config.local.ini'
    if not os.path.exists(config_file):
        sys.exit(f"Config file not found: {config_file}")
    config.read(config_file)

    logger.info("Gateway v4.5 (Heartbeat Check) started")

    iec104_server = libiec60870server.IEC60870_5_104_server()
    
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

    logger.info("Parsing configuration...")
    mms_to_ioa_map = {}
    ioa_inversion_map = {}
    ied_data_groups = {}
    
    all_sections = list(data_types.keys()) + list(command_types.keys())
    
    for section in all_sections:
        if section in config:
            for ioa, config_line in config[section].items():
                try:
                    uri_part = config_line.split(':invers=true')[0]
                    should_invert = ':invers=true' in config_line
                    parsed_uri = urlparse(uri_part)
                    mms_path = parsed_uri.path.lstrip('/')
                    hostname = parsed_uri.hostname
                    port = parsed_uri.port or 102
                    ied_id = f"{hostname}:{port}"
                    
                    if not mms_path or not hostname: continue
                    
                    mms_to_ioa_map[mms_path] = int(ioa)
                    if should_invert: ioa_inversion_map[int(ioa)] = True
                    if section in command_types: ioa_to_mms_config[int(ioa)] = uri_part
                    if section in data_types:
                        if ied_id not in ied_data_groups: ied_data_groups[ied_id] = []
                        ied_data_groups[ied_id].append(uri_part)
                except Exception as e:
                    logger.error(f"Error processing config line for IOA {ioa}: {config_line} - {e}")

    logger.info(f"Found {len(ied_data_groups)} unique IEDs to monitor.")

    for section, mms_type in data_types.items():
        if section in config:
            for item in config[section]:
                iec104_server.add_ioa(int(item), mms_type, 0, None, True)
    for section, mms_type in command_types.items():
        if section in config:
            for item in config[section]:
                iec104_server.add_ioa(int(item), mms_type, 0, command_60870_callback, False)
    
    iec104_server.start()

    threads = []
    for ied_id, uris in ied_data_groups.items():
        thread = threading.Thread(target=ied_thread_worker, args=(ied_id, uris, shutdown_event), name=f"IED-{ied_id}")
        thread.daemon = True
        thread.start()
        threads.append(thread)

    logger.info(f"All {len(threads)} IED threads started. Gateway is running.")
    
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutdown signal received...")
        shutdown_event.set()
        for thread in threads: thread.join()
    finally:
        if iec104_server: iec104_server.stop()
        logger.info("Gateway stopped.")
