# 📂 Python Simple NAS Server

A lightweight, multi-threaded **Network Attached Storage (NAS)** solution written in a single Python script.  
Transform any directory into a web-based file management hub accessible from any device on your **local network**.

---

## ✨ Key Features

- 🌐 **Web Interface:** Clean, responsive UI for managing files from any browser (mobile & desktop).  
- 📤 **High-Speed Uploads:** Supports multiple file uploads with a real-time progress bar, speed tracker (KB/s), and time estimates.  
- 📥 **Reliable Downloads:** Integrated download progress tracking for large files.  
- 🛡️ **Admin-Verified Deletion:**  
  Users can request deletion — the server admin must manually type `yes` in the terminal to confirm.  
  Prevents accidental or malicious data loss.  
- 🔒 **Built-in Protection:**  
  The server script itself is hidden from the web UI.  
  Protected files cannot be downloaded, overwritten, or deleted.  
- 🚀 **Zero Dependencies:** Works out of the box with standard Python 3.7+ libraries (handles CGI deprecation in newer Python versions).  
- 🧵 **Multi-threaded:** Handles multiple users uploading/downloading simultaneously.  

---

## 🚀 Quick Start

### 1. Requirements
- **Python 3.7 or higher**
- **No external libraries** (no `pip install` required)

### 2. Launch the Server
Place the script in the folder you wish to share and run:

```
python simple_nas.py
```

### 3. Accessing the NAS
Once started, the terminal will display your local addresses:

```
Local:   http://localhost:8000
Network: http://192.168.x.x:8000
```

Use the **Network (LAN)** address to access from your phone or other PCs.

---

## 🛠️ Configuration

You can edit these variables at the top of the `simple_nas.py` file:

| Variable | Description |
|-----------|-------------|
| **PORT** | The port the server listens on. *(Default: 8000)* |
| **DIRECTORY** | The root folder to share. *(Default: current directory)* |
| **MAX_UPLOAD_SIZE** | Set a limit in bytes to prevent disk filling. *(Default: None)* |
| **PROTECTED_FILES** | List of files to hide and lock from the web interface. |

---

## ⚠️ Important Notes

- 🔒 **Security:** Intended for **Local Area Network (LAN)** use only.  
  Do **not** expose the port to the public internet (port forwarding) without a password-protected reverse proxy (e.g., Nginx).  
- 🧑‍💻 **Admin Interaction:** The deletion process requires the admin to be present at the server terminal to approve delete requests.  
- 🧩 **Compatibility:** Includes a custom fallback parser for `multipart/form-data`, ensuring compatibility with Python 3.13 and beyond (where the `cgi` module is removed).  

---

## 🖥️ UI Screenshots

The web interface features a modern table view with:
- 📁 Folder navigation  
- 📊 Human-readable file sizes (KB, MB, GB)  
- 🟢 Real-time progress indicators for all network activity  

---

## 📜 License

**Open-source and free for personal use.**  
Feel free to modify it for your specific needs!
```
