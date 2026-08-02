# QRLAN Drop

**Current version: 1.5.0** · [Read the patch notes](CHANGELOG.md)

<img width="922" height="752" alt="QRLAN Drop v1.5 dark interface" src="https://github.com/user-attachments/assets/d2e6eedf-22b0-49cd-9ca9-596042ca03c9" />

Send files between a Windows 11 computer and a phone by scanning a QR code.

No account. No cloud upload. Both devices only need to be on the same Wi-Fi.

## What is new in version 1.5

- A modern dark design that matches Windows 11.
- Smooth button, progress-bar, and status-light animations.
- **Open Folder** opens the folder containing your file.
- **Open File** opens the selected or newest received file.
- No extra command window and no bright white title bar.
- Choose your receive folder and watch progress for large files.
- No artificial file-size limit.

## Easy download

1. Open the [latest release](https://github.com/Dhanendra-github/qrlan-drop/releases/latest).
2. Under **Assets**, download **QRLAN.Drop.exe**.
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

Use **Open Folder** to see where the file is saved. Use **Open File** to open it immediately.

## Send a file from the phone

1. Open **QRLAN Drop**.
2. Click **Receive**.
3. Click **Choose receive folder...** and pick where the file should be saved.
4. Click **Create upload QR**.
5. Scan the QR code with your phone's camera.
6. Choose a file on the phone.
7. Tap **Upload file**.

Keep the app open until the progress bar reaches 100%.

Use **Open Folder** to see the received file. Use **Open File** to open the newest received file.

## Important

- The computer and phone must be connected to the same Wi-Fi.
- Some guest Wi-Fi networks block devices from talking to each other.
- There is no artificial file-size limit. Free disk space and Wi-Fi stability are the practical limits.
- Click **Stop transfer** when finished. This makes the QR link stop working.
- Files stay on your local network. They are not uploaded to a cloud service.
- The QR link only works while QRLAN Drop is open.

## Version 1.5 downloads

- [Download the Windows app](https://github.com/Dhanendra-github/qrlan-drop/releases/download/v1.5.0/QRLAN.Drop.exe)
- [Download the source code](https://github.com/Dhanendra-github/qrlan-drop/releases/download/v1.5.0/QRLAN.Drop.Source.zip)
- [Read all version 1.5 patch notes](CHANGELOG.md)

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
