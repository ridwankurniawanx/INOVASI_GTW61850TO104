#!/usr/bin/env python3
# gateway_v4.2.py
# Deskripsi: Gateway IEC 61850 ke IEC 60870-5-104 multi-threaded
# Fitur v4.2: Menambahkan logika reconnect yang tangguh untuk setiap koneksi IED.

import socket
import json
import subprocess
import time
import sys
import os
import logging
import configparser
import threading
from urllib.parse import urlparse

# --- MODIFIKASI: Impor pustaka klien yang sudah di-cache ---
import libiec61850client_cached as libiec61850client
import libiec60870server
import lib60870

# --- MODIFIKASI: Variabel global untuk threading ---
# Kamus untuk menyimpan instance client per IED, dilindungi oleh Lock
ied_clients = {}
client_lock = threading.Lock()
# Kamus untuk memetakan IOA kembali ke konfigurasi URI-nya, untuk menemukan IED yang tepat saat ada perintah
ioa_to_mms_config = {}
# Event untuk memberi sinyal shutdown ke semua thread
shutdown_event = threading.Event()

# Konfigurasi
hosts_info = {}
async_msg = []
async_rpt = {}
INTERVAL = 0.1
# --- BARU: Jeda waktu sebelum mencoba koneksi ulang ke IED yang gagal ---
RECONNECT_DELAY = 15 # dalam detik

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
    # Fungsi ini sekarang bersifat konseptual karena pembacaan ditangani oleh polling
    return 0, "not directly supported in threaded model"

def write_value(client, id, value):
    logger.debug(f"write value: {value}, element: {id} using client {client}")
    retValue, msg = client.registerWriteValue(str(id), str(value))
    if retValue > 0:
        return retValue, libiec61850client.IedClientError(retValue).name
    if retValue == 0:
        return retValue, "no error"
    return retValue, "general error"

def operate(client, id, value):
    logger.debug(f"operate: {id} v: {value} using client {client}")
    val_str = "true" if value == 1 else "false"
    return client.operate(str(id), val_str)


def select(client, id, value):
    logger.debug(f"select: {id} using client {client}")
    val_str = "true" if value == 1 else "false"
    return client.select(str(id), val_str)


def cancel(client, id):
    logger.debug(f"cancel: {id} using client {client}")
    return client.cancel(str(id))

# FUNGSI CALLBACK YANG SUDAH DIOPTIMASI DAN FLEKSIBEL
def readvaluecallback(key, data):
    global iec104_server, ioa_inversion_map, mms_to_ioa_map
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

                should_invert = ioa_inversion_map.get(ioa, False)

                if ioa_type == lib60870.DoublePointInformation:
                    val_map = {1.0: 1, 2.0: 2}
                    dp_val = val_map.get(numeric_value, 0)
                    if should_invert:
                        if dp_val == 1: value_to_send = 2
                        elif dp_val == 2: value_to_send = 1
                        else: value_to_send = dp_val
                    else:
                        value_to_send = dp_val

                elif ioa_type == lib60870.SinglePointInformation:
                    sp_val = 1 if int(numeric_value) != 0 else 0
                    if should_invert:
                        value_to_send = 1 - sp_val
                    else:
                        value_to_send = sp_val

                # NOTE: Asumsi iec104_server.update_ioa() adalah thread-safe.
                # Jika tidak, lock perlu ditambahkan di sini.
                iec104_server.update_ioa(ioa, value_to_send)
                logger.info(f"Successfully matched '{reported_key}' to '{config_path}' and updated IOA {ioa} with value {value_to_send}")
                found_match = True
                break
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
    # Pembacaan spontan sekarang ditangani oleh readvaluecallback.
    # GI (General Interrogation) akan mengembalikan nilai terakhir yang di-cache di server 104.
    logger.debug(f"General interrogation for IOA {ioa}. Value will be read from 104 server cache.")
    return 0


def command_60870_callback(ioa, ioa_data, iec104server, select_value):
    global ioa_to_mms_config, ied_clients, client_lock
    logger.debug(f"Command callback for IOA {ioa}, select: {select_value}")

    # 1. Temukan URI konfigurasi berdasarkan IOA
    config_line = ioa_to_mms_config.get(ioa)
    if not config_line:
        logger.error(f"No configuration found for command IOA {ioa}")
        return -1

    # 2. Parse URI untuk mendapatkan ID IED (host:port)
    try:
        parsed_uri = urlparse(config_line)
        ied_id = f"{parsed_uri.hostname}:{parsed_uri.port or 102}"
        mms_path = parsed_uri.path.lstrip('/')
    except Exception as e:
        logger.error(f"Could not parse URI '{config_line}' for IOA {ioa}: {e}")
        return -1

    # 3. Dapatkan client yang sesuai untuk IED ini secara thread-safe
    with client_lock:
        client = ied_clients.get(ied_id)

    if not client:
        logger.error(f"No active client found for IED '{ied_id}' to handle command for IOA {ioa}. IED may be offline.")
        return -1

    # 4. Jalankan perintah menggunakan client yang benar
    logger.info(f"Routing command for IOA {ioa} to IED '{ied_id}' at path '{mms_path}'")
    if select_value:
        return select(client, mms_path, ioa_data['data'])
    else:
        return operate(client, mms_path, ioa_data['data'])

# --- MODIFIKASI TOTAL: Fungsi worker untuk setiap thread IED dengan logika Reconnect ---
def ied_thread_worker(ied_id, uris_to_register, stop_event):
    """
    Fungsi yang akan dijalankan oleh setiap thread.
    Menangani satu koneksi IED dan mencoba menyambung kembali jika terputus.
    """
    hostname, port = ied_id.split(':')
    logger.info(f"Starting thread for IED at {ied_id}")

    client = None

    while not stop_event.is_set():
        # Jika tidak terkoneksi (client is None), coba untuk terhubung
        if client is None:
            logger.info(f"[{ied_id}] Attempting to connect...")
            try:
                # 1. Buat instance client baru
                new_client = libiec61850client.iec61850client(readvaluecallback, logger, cmdTerm_cb, Rpt_cb)

                # 2. Daftarkan semua datapoint untuk IED ini
                logger.info(f"[{ied_id}] Registering {len(uris_to_register)} data points...")
                for uri in uris_to_register:
                    new_client.registerReadValue(str(uri))
                new_client.getRegisteredIEDs()

                # 3. Jika berhasil, simpan client di kamus global
                with client_lock:
                    ied_clients[ied_id] = new_client
                client = new_client # Simpan client untuk loop polling
                logger.info(f"[{ied_id}] Connection successful. Polling started.")

            except Exception as e:
                logger.warning(f"[{ied_id}] Connection failed: {e}. Retrying in {RECONNECT_DELAY}s.")
                # Pastikan client dihapus dari kamus global jika koneksi gagal
                with client_lock:
                    if ied_id in ied_clients:
                        del ied_clients[ied_id]

                # Tunggu sebelum mencoba lagi, tapi tetap responsif terhadap sinyal shutdown
                stop_event.wait(RECONNECT_DELAY)
                continue # Kembali ke awal loop untuk mencoba koneksi lagi

        # Jika sudah terkoneksi, lakukan polling
        try:
            if client:
                client.poll()
                logger.debug(f"[{ied_id}] Values polled")
                time.sleep(INTERVAL) # Jeda singkat antar polling

        except Exception as e:
            logger.error(f"[{ied_id}] Connection lost: {e}. Will attempt to reconnect.", exc_info=False)
            # 1. Hapus client dari kamus global secara aman
            with client_lock:
                if ied_id in ied_clients:
                    del ied_clients[ied_id]

            # 2. Set client lokal menjadi None agar loop berikutnya mencoba koneksi ulang
            client = None
            # Loop akan otomatis mencoba koneksi ulang pada iterasi berikutnya

    # Cleanup saat shutdown_event di-set
    logger.info(f"Thread for IED {ied_id} is stopping.")
    with client_lock:
        if ied_id in ied_clients:
            del ied_clients[ied_id]


if __name__ == '__main__':
    logger = logging.getLogger('gateway')
    logging.basicConfig(format='%(asctime)s [%(threadName)-12s] %(name)-12s %(levelname)-8s %(message)s',
                        level=logging.INFO)

    config = configparser.ConfigParser()
    config.optionxform = str

    config_file = sys.argv[1] if len(sys.argv) > 1 else 'config.local.ini'
    if not os.path.exists(config_file):
        logger.error(f"Config file not found: {config_file}")
        sys.exit(1)
    config.read(config_file)

    logger.info("Gateway v4.2 (Multi-Threaded, Reconnect) started")

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

    # --- MODIFIKASI: Parsing dan pengelompokan konfigurasi per IED ---
    logger.info("Parsing configuration and grouping by IED...")
    mms_to_ioa_map = {}
    ioa_inversion_map = {}
    ied_data_groups = {} # Kamus untuk -> {'host:port': [uri1, uri2, ...]}

    all_sections = list(data_types.keys()) + list(command_types.keys())

    for section in all_sections:
        if section in config:
            for ioa, config_line in config[section].items():
                try:
                    uri_part = config_line
                    should_invert = False

                    marker = ':invers=true'
                    if config_line.endswith(marker):
                        uri_part = config_line[:-len(marker)]
                        should_invert = True

                    parsed_uri = urlparse(uri_part)
                    mms_path = parsed_uri.path.lstrip('/')
                    hostname = parsed_uri.hostname
                    port = parsed_uri.port or 102 # Default port 102 jika tidak ada
                    ied_id = f"{hostname}:{port}"

                    if not mms_path or not hostname:
                        logger.warning(f"Could not parse path/host from URI: '{uri_part}' for IOA {ioa}. Skipping.")
                        continue

                    # Isi map untuk pemetaan dan inversi
                    mms_to_ioa_map[mms_path] = int(ioa)
                    if should_invert:
                        ioa_inversion_map[int(ioa)] = True

                    # Isi map untuk callback perintah
                    if section in command_types:
                        ioa_to_mms_config[int(ioa)] = uri_part

                    # Kelompokkan URI berdasarkan IED untuk data monitoring
                    if section in data_types:
                        if ied_id not in ied_data_groups:
                            ied_data_groups[ied_id] = []
                        ied_data_groups[ied_id].append(uri_part)

                except Exception as e:
                    logger.error(f"Error processing config line for IOA {ioa}: {config_line} - {e}")

    logger.info(f"Found {len(ied_data_groups)} unique IEDs to monitor.")
    logger.debug(f"MMS->IOA Map: {mms_to_ioa_map}")
    logger.debug(f"Inversion Map: {ioa_inversion_map}")
    logger.debug(f"IOA->MMS Command Map: {ioa_to_mms_config}")

    # Daftarkan semua IOA ke server 104 SEBELUM memulai thread
    logger.info("Registering all IOAs to the 104 server...")
    for section, mms_type in data_types.items():
        if section in config:
            for item in config[section]:
                if iec104_server.add_ioa(int(item), mms_type, 0, read_60870_callback, True) != 0:
                    logger.error(f"Duplicate data IOA: {item}, IOA not added to list")

    for section, mms_type in command_types.items():
        if section in config:
            for item in config[section]:
                if iec104_server.add_ioa(int(item), mms_type, 0, command_60870_callback, False) == 0:
                    logger.info(f"{mms_type.__name__} registered for IOA {item}")
                else:
                    logger.error(f"Duplicate command IOA: {item}, IOA not added to list")

    # Mulai server 104
    iec104_server.start()

    # --- MODIFIKASI: Buat dan mulai satu thread untuk setiap IED ---
    threads = []
    for ied_id, uris in ied_data_groups.items():
        thread_name = f"IED-{ied_id}"
        thread = threading.Thread(target=ied_thread_worker, args=(ied_id, uris, shutdown_event), name=thread_name)
        thread.daemon = True # Thread akan otomatis berhenti jika main program berhenti
        thread.start()
        threads.append(thread)

    logger.info(f"All {len(threads)} IED threads have been started. Gateway is running.")

    try:
        # Jaga thread utama tetap hidup untuk menangani sinyal shutdown
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nCtrl+C received. Shutting down gateway gracefully...")
        logger.info("Shutdown signal sent to all threads.")
        shutdown_event.set()

        # Tunggu semua thread selesai
        for thread in threads:
            thread.join()

    except Exception as e:
        logger.error(f"An unexpected error occurred in the main thread: {e}", exc_info=True)
        shutdown_event.set()
    finally:
        if iec104_server:
            iec104_server.stop()
        logger.info("All threads have been joined. Gateway stopped.")
