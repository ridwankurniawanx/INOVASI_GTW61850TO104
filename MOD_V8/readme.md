| Fitur | gateway_v8.0.py | gateway_v8.1.py |
| :--- | :--- | :--- |
| **Metode Ekstraksi Nilai** | Hanya menggunakan `find_first_float` untuk mencari nilai numerik (float/int) pertama secara rekursif. | Memperkenalkan fungsi baru `get_value_by_path` untuk mengekstrak nilai dari path spesifik (misal: `mag.f`). Jika path tidak ada, baru menggunakan `find_first_float` sebagai *fallback*. |
| **Konfigurasi (`config.local.ini`)** | Menginterpretasikan seluruh baris sebagai URI tanpa parsing tambahan untuk path nilai. | Mampu mem-parsing baris yang mengandung karakter `#` untuk memisahkan URI utama dengan path nilai spesifik. |
| **Fleksibilitas & Presisi** | Kurang fleksibel. Mengambil nilai numerik pertama yang ditemui, yang mungkin bukan nilai yang diinginkan jika ada beberapa angka. | **Lebih presisi dan fleksibel**. Pengguna dapat menunjuk langsung ke *data point* yang diinginkan, memastikan nilai yang benar yang diambil dari struktur data yang kompleks. |
| **Logika Pemrosesan Data** | Fungsi `process_data_update` hanya memiliki satu cara untuk mendapatkan nilai, yaitu dengan `find_first_float`. | Fungsi `process_data_update` dimodifikasi secara signifikan untuk memeriksa apakah ada path nilai spesifik yang dikonfigurasi sebelum memutuskan metode ekstraksi mana yang akan digunakan. |
| **Variabel Global Baru** | Tidak ada. | Menambahkan `mms_to_value_path_map` untuk menyimpan pemetaan dari path MMS ke path nilai spesifik di dalam struktur datanya. |

---

### Kesimpulan

Pembaruan dari v8.0 ke v8.1 adalah **peningkatan fungsionalitas yang signifikan**. `gateway_v8.1.py` jauh lebih andal karena memberikan kontrol penuh kepada pengguna untuk menentukan secara tepat nilai mana yang akan diambil dari setiap titik data IEC 61850, sementara tetap mempertahankan mekanisme *fallback* dari v8.0 untuk kompatibilitas.
