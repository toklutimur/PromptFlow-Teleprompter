# PromptFlow Studio

Yerel çalışan, sesle senkronize bir teleprompter. Bir ses dosyasını sürükleyip bırakın; PromptFlow konuşmayı yerel Whisper modeliyle analiz eder, metni üretir ve her kelimeyi ses içindeki gerçek zamanına bağlar.

![PromptFlow Studio](docs/screenshots/promptflow-studio.png)

## Neden PromptFlow?

Sabit WPM tabanlı teleprompterlar vurgu, kelime uzatma, nefes ve duraklamalarda konuşmacının gerisinde kalır. PromptFlow ses dosyasını ana zaman kaynağı olarak kullanır:

- Kelime seviyesinde zaman damgası çıkarır.
- Vurgu ve düzensiz konuşma hızlarını takip eder.
- Uzun metni okunabilir parçalara bölerek ekranda aşağı doğru kaydırır.
- Ses ve metni yerel bilgisayarda işler; buluta ses göndermez.
- Metin sesle uyuşmuyorsa, sesin transkriptini otomatik olarak kullanabilir.

## Özellikler

- MP3, WAV, M4A ve OGG sürükle-bırak desteği
- Türkçe dahil otomatik dil algılama
- Whisper `large-v3-turbo` ile kelime zamanlaması
- Ses dosyasını prompter ile birlikte oynatma
- Gerçek zamanlı kelime vurgusu
- Odak çizgili, tam ekran okuma modu
- Ayna görünümü
- Yerel metin kaydetme ve TXT dışa aktarma
- Tamamen lokal çalışma

## Hızlı başlangıç — Windows

Gereksinimler:

- Windows 10/11
- Python 3.12
- FFmpeg ve PATH üzerinde `ffmpeg`
- İnternet bağlantısı: yalnızca ilk Whisper model indirmesi için

1. Depoyu klonlayın veya GitHub’dan indirin.
2. Proje klasöründe sanal ortam oluşturun ve bağımlılığı yükleyin:

   ```powershell
   uv venv .venv312 --python 3.12
   uv pip install --python .venv312\Scripts\python.exe faster-whisper
   ```

3. `start-promptflow.bat` dosyasını çalıştırın.
4. Açılan tarayıcı penceresinde konuşma metnini temizleyin.
5. Ses dosyasını `MP3 bırak / seç` alanına sürükleyin.
6. `Metin sesten oluşturuldu ✓` mesajını bekleyin.
7. `▶` düğmesine basın.

İlk analizde model indirileceği için işlem daha uzun sürebilir. Sonraki analizler yerel modelden yapılır.

## Mevcut metinle çalışma

Ses dosyasına ait metin zaten elinizdeyse metin alanına yapıştırabilirsiniz. Sesle metin yeterince eşleşirse PromptFlow mevcut metni koruyup zaman damgalarını ona bağlar. Eşleşme düşükse ses transkriptini kullanır.

Metni analizden sonra değiştirirseniz MP3’ü tekrar sürükleyerek senkronu yenileyin.

## Proje yapısı

```text
PromptFlow-Teleprompter/
├── index.html                  # Teleprompter arayüzü ve istemci kodu
├── align_server.py             # Yerel Whisper analiz sunucusu
├── start-promptflow.bat        # Windows başlatıcısı
├── requirements.txt            # Python bağımlılığı
├── LICENSE                     # MIT lisansı
└── docs/screenshots/           # README görselleri
```

## Mimari

```text
Ses dosyası
    │
    ├── FFmpeg → 16 kHz mono PCM
    │
    ├── faster-whisper large-v3-turbo
    │       └── kelime + başlangıç/bitiş zamanları
    │
    ├── metin eşleştirme
    │
    └── tarayıcı teleprompteri
            ├── ses ana saat
            ├── kelime vurgusu
            └── otomatik odak kaydırma
```

`align_server.py` yalnızca `127.0.0.1:8765` üzerinde çalışır. Ses ve transkript uzak bir servise gönderilmez.

## Notlar

- `large-v3-turbo` ilk çalıştırmada Hugging Face üzerinden indirilir ve yerel önbellekte tutulur.
- Daha iyi sonuç için ses temiz, tek konuşmacılı ve metinle uyumlu olmalıdır.
- Otomatik transkript, özellikle gürültülü veya çok hızlı kayıtlarda manuel kontrol gerektirebilir.
- GitHub’a sanal ortamlar, model önbelleği, kullanıcı sesleri ve çalışma çıktıları dahil edilmez.

## Lisans

MIT — ayrıntılar için [LICENSE](LICENSE) dosyasına bakın.
