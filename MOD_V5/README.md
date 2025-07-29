# Gateway IEC 61850 ke IEC 104 (v5.7 - Asyncio)

## Deskripsi

Gateway multi-koneksi yang tangguh untuk mentranslasikan data dari beberapa IED IEC 61850 ke satu server IEC 60870-5-104. Versi ini telah dimigrasikan sepenuhnya ke arsitektur **`asyncio`** untuk performa, skalabilitas, dan stabilitas yang tinggi, terutama saat menangani banyak IED.

---

## ✨ Fitur Utama

Versi ini dirancang untuk menjadi sangat tangguh (*robust*) dan andal di lingkungan produksi:

* **Arsitektur Asynchronous (`asyncio`)**
    Menggunakan satu *thread* untuk menangani banyak koneksi IED secara efisien. Ini secara drastis mengurangi penggunaan memori dan CPU dibandingkan model *multi-threading* dan mampu menangani ratusan IED secara bersamaan.

* **Isolasi Kegagalan (Lock Per-IED)**
    Satu IED yang lambat, bermasalah, atau sedang dalam proses *reconnect* yang lama **tidak akan pernah memblokir atau mengganggu** komunikasi dengan IED lain yang sehat. Setiap IED beroperasi secara independen.

* **Monitoring Koneksi Proaktif**
    Gateway secara aktif memeriksa status koneksi TCP setiap IED secara berkala. Ia tidak hanya menunggu operasi `poll` gagal, sehingga koneksi yang terputus secara diam-diam (misal: kabel dicabut) dapat dideteksi dengan cepat.

* **Inwalidasi Data Otomatis**
    Ketika koneksi IED terdeteksi putus, semua titik datanya di sisi server 104 akan secara otomatis dan seketika ditandai dengan kualitas **`INVALID`**. Ini memberikan sinyal yang jelas ke sistem SCADA bahwa data tersebut tidak lagi bisa dipercaya.

* **Sinkronisasi Ulang Otomatis saat Pulih**
    Segera setelah koneksi IED berhasil pulih, gateway akan secara proaktif **membaca ulang semua nilai terkini** dari IED tersebut. Ini memastikan data di sisi SCADA segera kembali sinkron dengan status `VALID`. SCADA tidak perlu melakukan *General Interrogation* manual.

* **Callback Berkonteks (Anti *Cross-Talk*)**
    Alur pemrosesan data kini "sadar" dari IED mana data berasal. Ini mencegah *bug* di mana data dari satu IED (misalnya IED-A) bisa salah dipetakan ke IOA milik IED lain (IED-B), sehingga menjamin integritas data.

---

## 🏗️ Arsitektur

Gateway v5 menggunakan pola desain **Producer-Consumer**:
1.  **Producer**: Setiap IED dikelola oleh sebuah *Task* `asyncio` (`ied_handler`). Task ini bertindak sebagai produsen yang membaca data dari IED dan memasukkannya ke dalam satu antrian (`asyncio.Queue`) terpusat.
2.  **Consumer**: Sebuah *Task* tunggal (`iec104_processor`) bertindak sebagai konsumen. Ia mengambil pesan (baik pembaruan data maupun permintaan inwalidasi) dari antrian satu per satu dan berinteraksi dengan server 104.

Pola ini secara inheren bersifat *thread-safe* untuk interaksi dengan server 104, menghilangkan kemungkinan *race condition* yang kompleks.

---

## ⚙️ Konfigurasi

* Semua konfigurasi IED dan pemetaan IOA diatur dalam file `config.local.ini`.
* Parameter penting di dalam skrip `gateway_v5.7.py` yang bisa disesuaikan:
    * `RECONNECT_DELAY`: Waktu tunggu (dalam detik) sebelum mencoba menyambung kembali ke IED yang gagal.
    * `CONNECTION_CHECK_INTERVAL`: Seberapa sering (dalam detik) status koneksi setiap IED diperiksa secara proaktif.

---

## ▶️ Cara Menjalankan

Jalankan skrip dari terminal dengan menunjuk ke file konfigurasi Anda:

```bash
python3 gateway_v5.7.py config.local.ini
