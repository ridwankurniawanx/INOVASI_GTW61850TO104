# Gateway Protokol IEC 61850 ke IEC 104

Gateway ini berfungsi sebagai jembatan protokol, membaca data dari perangkat IEC 61850 (seperti IED atau relay proteksi) melalui protokol MMS dan menyajikannya kepada sistem SCADA atau Master Station melalui protokol IEC 60870-5-104.

Aplikasi ini ditulis dengan Python dan dirancang untuk menjadi fleksibel dan tangguh untuk lingkungan industri.

---

## Fitur Utama

-   **Konversi Protokol**: Menerjemahkan data point dari model data IEC 61850 ke Information Object Address (IOA) IEC 104.
-   **Konfigurasi Eksternal**: Semua pemetaan titik data didefinisikan dalam file `.ini`, sehingga tidak perlu mengubah kode untuk mengubah pemetaan.
-   **Inversi Nilai**: Mendukung pembalikan nilai untuk data `SinglePointInformation` dan `DoublePointInformation` langsung dari file konfigurasi.
-   **Penanganan Error**: Didesain untuk terus berjalan meskipun beberapa IED (perangkat 61850) sedang offline saat startup.
-   **Struktur Berbasis Kelas**: Kode diatur dalam struktur kelas yang rapi untuk kemudahan pemeliharaan dan pengujian.

---

## Konfigurasi (`config.local.ini`)

Konfigurasi adalah jantung dari gateway ini. Formatnya sederhana dan mudah dipahami.

```ini
# Ini adalah contoh file config.local.ini

[measuredvaluefloat]
# Format: IOA = URI # Nama Sinyal (komentar)
3073 = iec61850://10.10.22.82:102/BCUULEE2MEASUREMENT1/powMMXU1.TotW.mag.f # Daya Aktif Total

[doublepointinformation]
# Format untuk nilai normal:
4097 = iec61850://10.175.98.226:102/BCUULEE2CONTROL1/CSWI1.Pos.stVal # Posisi PMT Masuk 1

# Format untuk nilai terbalik (inversi):
# Tambahkan `:invers=true` di akhir URI tanpa spasi.
4098 = iec61850://10.175.98.226:102/BCUULEE2CONTROL1/CSWI2.Pos.stVal:invers=true # Posisi PMT Keluar 1 (INV)
