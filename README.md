# QRLAN Drop

Send files between a Windows 11 computer and a phone by scanning a QR code.

No account. No cloud upload. Both devices only need to be on the same Wi-Fi.

## Easy download

1. Open the [latest release](https://github.com/Dhanendra-github/qrlan-drop/releases/latest).
2. Download **QRLAN Drop.exe**.
3. Double-click the downloaded file.
4. If Windows Firewall asks, allow **Private networks**.

You do not need to install Python.

## Send a file from the computer

1. Open **QRLAN Drop**.
2. Click **Send**.
3. Click **Choose file**.
4. Pick the file you want to send.
5. Click **Create download QR**.
6. Scan the QR code with your phone's camera.
7. Tap **Download file** on the phone.

Keep the app open until the progress bar reaches 100%.

## Send a file from the phone

1. Open **QRLAN Drop**.
2. Click **Receive**.
3. Click **Choose receive folder...** and pick where the file should be saved.
4. Click **Create upload QR**.
5. Scan the QR code with your phone's camera.
6. Choose a file on the phone.
7. Tap **Upload file**.

Keep the app open until the progress bar reaches 100%.

## Important

- The computer and phone must be connected to the same Wi-Fi.
- Some guest Wi-Fi networks block devices from talking to each other.
- There is no artificial file-size limit. Free disk space and Wi-Fi stability are the practical limits.
- Click **Stop transfer** when finished. This makes the QR link stop working.
- Files stay on your local network. They are not uploaded to a cloud service.

## Build it yourself

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/).
2. Download this repository and unzip it.
3. Right-click **build.ps1** and choose **Run with PowerShell**.
4. Find the finished app at `dist\QRLAN Drop.exe`.

Run the tests with:

```powershell
python -m unittest -v
```

## License

MIT License. You may use, copy, change, and share the project. See [LICENSE](LICENSE).
