# Goukta Python Eğitim Editörü

Bu mini proje tek sayfalık bir Python çalışma ekranı sağlar:

- Üst bölüm: Python kodunu yazarsın.
- `Çalıştır` butonu: Kodu çalıştırır.
- Alt bölüm: Çıktıyı gösterir.

## Çalıştırma

```bash
cd python-playground
python3 server.py
```

Tarayıcıda aç:

`http://127.0.0.1:8000`

## Telefonda Yerel Ağda Kullanım (Aynı Wi-Fi)

1. Bilgisayar ve telefonu aynı Wi-Fi ağına bağla.
2. Bilgisayarda sunucuyu başlat: `python3 server.py`
3. Bilgisayarın IP adresini öğren (Mac):
   - Wi-Fi için: `ipconfig getifaddr en0`
4. Telefonda şu adresi aç:
   - `http://BILGISAYAR_IP:8000`
   - Örnek: `http://192.168.1.24:8000`

## İnternetten Canlı Yayın (Öğrenciler Evden Girsin)

En pratik yol: Render veya Railway gibi bir servis.

Render örnek adımları:

1. Projeyi GitHub'a yükle.
2. [Render](https://render.com/) içinde yeni bir `Web Service` oluştur.
3. Depoyu bağla ve servis klasörü olarak `python-playground` seç.
4. `Start Command` alanına şunu yaz: `python3 server.py`
5. Deploy sonrası verilen `https://...onrender.com` linkini öğrencilerle paylaş.

Not: Kod `PORT` değişkenini otomatik okuyacak şekilde ayarlanmıştır, bu yüzden bulutta ek ayar gerektirmez.

## Güvenlik Notu

Bu uygulama eğitim amaçlıdır ve sunucuya gönderilen Python kodunu çalıştırır.
İnternete açacaksan en azından şunları ekle:

- basit giriş şifresi (öğretmen/öğrenci erişimi),
- istek başına süre ve çıktı limiti (zaten var),
- günlük istek limiti (rate limit).
