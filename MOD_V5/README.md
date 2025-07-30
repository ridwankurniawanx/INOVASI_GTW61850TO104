# MOD_V5 Gateway IEC 61850 <-> IEC 104

## Versi & Fitur

### gateway_v5.7.py
- Migrasi penuh ke arsitektur asyncio, scalable untuk banyak IED.
- Koneksi proaktif dan auto-reconnect.
- Konfigurasi interval dan parameter melalui `config.local.ini`.

### gateway_v5.9.py
- Final robust release untuk RockyLinux.
- Keamanan thread & event loop: semua objek asinkron dibuat di dalam event loop utama.
- Logging yang lebih informatif dan shutdown event yang aman.

### gateway_v7.1.py
- Memperbaiki masalah event pada loop berbeda.
- Inisialisasi objek asinkron yang benar dan logging sumber.
- Siap untuk troubleshooting dan pengembangan lebih lanjut.

### gateway_v7.2.py
- Penambahan fitur polling interval adaptif.
- Mengurangi polling jika report sukses, mempercepat polling jika gagal.
- Efisiensi dan performa untuk sistem dengan banyak IED reporting.

---

## Cara Menjalankan
```bash
python3 gateway_vX.X.py config.local.ini
