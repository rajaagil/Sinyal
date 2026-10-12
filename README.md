# Sinyal Pagi

Bot GitHub Actions yang setiap hari mengirim ke Discord:

- **06:20–07:00 WITA**: maksimal 3 koin per kategori, semua beli 07:00 WITA:
  - **12 jam**: jual 19:00 WITA
  - **1 hari**: jual besok 07:00 WITA
  - **2 hari**: jual lusa 07:00 WITA

  Tiap koin diberi perkiraan untung % (rata-rata historis setup yang sama di periode itu, setelah biaya), peluang naik, dan kerugian terburuk. Urutan berdasarkan perkiraan untung terbesar.
- **19:05 WITA**: hasil nyata semua sinyal yang periodenya sudah selesai.

Data: candle per jam 365 hari terakhir dari API publik Crypto.com Exchange (tanpa API key), 23 koin.

## Setup (sekali, sekitar 5 menit)

1. Buat repo baru di GitHub (boleh **Private**), lalu upload semua file di folder ini, termasuk folder `.github/workflows/`.
2. Di Discord: Server Settings → Integrations → Webhooks → New Webhook → pilih channel → **Copy Webhook URL**.
3. Di repo GitHub: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `DISCORD_WEBHOOK_URL`
   - Secret: tempel URL webhook tadi
4. Tab **Actions** → kalau diminta, klik "I understand my workflows, go ahead and enable them".
5. Tes: Actions → **Sinyal Pagi** → **Run workflow** → mode `test` → Run. Pesan tes harus muncul di Discord.
6. Tes analisis: Run workflow lagi dengan mode `morning` dan centang **dry_run**. Hasilnya tampil di log (tidak dikirim). Run pertama butuh beberapa menit untuk mengunduh 1 tahun data.

Setelah itu jadwal berjalan sendiri setiap hari.

## Pengaturan

- **Biaya per beli-jual**: default 0,4%. Untuk mengubah, buat *variable* (bukan secret) bernama `FEE_ROUND_TRIP` di Settings → Secrets and variables → Actions → Variables, misalnya `0.003` untuk 0,3%.
- **Daftar koin**: ubah `COINS` di `bot.py`.

## Catatan penting

- GitHub kadang menunda jadwal beberapa menit sampai belasan menit. Pesan pagi dijadwalkan 06:20 supaya biasanya tetap sampai sebelum 07:00.
- Harga di Pluang dalam Rupiah dan bisa sedikit berbeda dari Crypto.com. Yang dipakai adalah persentasenya.
- Repo yang tidak ada aktivitas 60 hari bisa membuat GitHub mematikan jadwal otomatis. Bot ini menyimpan rekam jejak ke folder `state/` setiap hari, jadi repo tetap aktif.
- "Perkiraan" adalah rata-rata masa lalu, bukan janji. Ini bukan saran keuangan.
