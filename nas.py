import http.server
import socketserver
import os
import urllib.parse
import shutil
import html
import tempfile
import time
import json
import uuid
from typing import Any

has_cgi: bool = False
cgi: Any = None
try:
    import cgi  # type: ignore
    has_cgi = True
except Exception:
    cgi = None  # type: ignore

PORT = 8000
DIRECTORY = os.getcwd()
MAX_UPLOAD_SIZE = NotImplemented

SCRIPT_NAME = os.path.basename(__file__)
PROTECTED_FILES = {SCRIPT_NAME, "simple_nas.py"}
PROTECTED_PATHS = { os.path.abspath(os.path.join(DIRECTORY, n)) for n in PROTECTED_FILES }

try:
    if os.name == "nt":
        import ctypes
        FILE_ATTRIBUTE_HIDDEN = 0x02
        for p in PROTECTED_PATHS:
            try:
                ctypes.windll.kernel32.SetFileAttributesW(str(p), FILE_ATTRIBUTE_HIDDEN)
            except Exception:
                pass
except Exception:
    pass

pending_deletes: dict[str, dict[str, Any]] = {}

class CustomRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Custom request handler to add upload capabilities."""
    
    def do_POST(self):
        """Handle file uploads and delete requests."""
        parsed = urllib.parse.urlparse(self.path)
        
        if parsed.path == '/delete':
            content_length = int(self.headers.get('content-length', 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode('utf-8'))
                filename = data.get('file', '')
                action = data.get('action', '')
            except Exception:
                self.send_json_response(400, {'error': 'Invalid JSON'})
                return
            
            if action == 'request':
                self._handle_delete_request(filename)
            elif action == 'confirm':
                token = data.get('token', '')
                self._handle_delete_confirm(filename, token)
            else:
                self.send_json_response(400, {'error': 'Invalid action'})
            return
        
        r, info = self.deal_post_data()
        print(r, info, "by: %s" % self.client_address[0])
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _handle_delete_request(self, filename: str) -> None:
        """Generate a delete token and ask admin for confirmation."""
        safe_name = os.path.basename(filename)

        if safe_name in PROTECTED_FILES:
            self.send_json_response(403, {'error': 'File is protected'})
            return

        file_path = os.path.join(DIRECTORY, safe_name)
        
        if not os.path.isfile(file_path):
            self.send_json_response(404, {'error': 'File not found'})
            return
        
        token = str(uuid.uuid4())
        client_ip = self.client_address[0]
        
        pending_deletes[token] = {
            'file': safe_name,
            'path': file_path,
            'ip': client_ip,
            'time': time.time()
        }
        
        print("\n" + "="*60)
        print(f"DELETE REQUEST from {client_ip}")
        print(f"File: {safe_name}")
        print(f"Token: {token}")
        print(f"Allow deletion? (yes/no): ", end='', flush=True)
        
        try:
            response = input().strip().lower()
            if response == 'yes':
                self._execute_delete(safe_name, file_path, token)
                self.send_json_response(200, {'status': 'File deleted', 'token': token})
                print(f"✓ File '{safe_name}' deleted by admin approval")
                del pending_deletes[token]
            else:
                self.send_json_response(403, {'error': 'Delete request denied by admin', 'token': token})
                print(f"✗ Delete request denied")
                del pending_deletes[token]
        except KeyboardInterrupt:
            self.send_json_response(500, {'error': 'Server interrupted'})
            print(f"\n✗ Delete request cancelled (server interrupted)")
            del pending_deletes[token]

    def _handle_delete_confirm(self, filename: str, token: str) -> None:
        """Handle client-side confirmation (if needed)."""
        if token not in pending_deletes:
            self.send_json_response(404, {'error': 'Invalid or expired token'})
            return
        
        req = pending_deletes[token]
        if req['file'] != os.path.basename(filename):
            self.send_json_response(400, {'error': 'File mismatch'})
            return
        
        self._execute_delete(req['file'], req['path'], token)
        self.send_json_response(200, {'status': 'File deleted'})
        del pending_deletes[token]

    def _execute_delete(self, filename: str, filepath: str, token: str) -> None:
        """Actually delete the file."""
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except Exception as e:
            print(f"Error deleting {filename}: {e}")

    def send_json_response(self, status_code: int, data: dict[str, Any]) -> None:  # type: ignore
        """Send a JSON response."""
        response = json.dumps(data).encode('utf-8')
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):
        """Handle download requests at /download?file=<name>, otherwise fall back."""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/download':
            qs = urllib.parse.parse_qs(parsed.query)
            if 'file' not in qs or not qs['file']:
                self.send_error(400, "Missing 'file' parameter")
                return
            fname = qs['file'][0]
            safe_name = os.path.basename(fname)

            if safe_name in PROTECTED_FILES:
                self.send_error(404, "File not found")
                return

            file_path = os.path.join(DIRECTORY, safe_name)
            if not os.path.isfile(file_path):
                self.send_error(404, "File not found")
                return
            try:
                ctype = self.guess_type(file_path)
                fs = os.path.getsize(file_path)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(fs))
                self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
                self.end_headers()
                with open(file_path, 'rb') as f:
                    shutil.copyfileobj(f, self.wfile)
            except Exception as e:
                self.send_error(500, f"Error serving file: {e}")
            return
        return super().do_GET()

    def deal_post_data(self) -> tuple[bool, str]:
        """Process the post data for file upload."""
        content_type = self.headers.get('content-type')
        if not content_type:
            return (False, "Content-Type header missing")
        if 'multipart/form-data' not in content_type:
            return (False, "Only multipart/form-data supported")

        try:
            content_length = int(self.headers.get('content-length', 0))
        except (TypeError, ValueError):
            content_length = 0
        if MAX_UPLOAD_SIZE and content_length > MAX_UPLOAD_SIZE:
            return (False, f"Upload too large (>{MAX_UPLOAD_SIZE / (1024*1024):.0f} MB)")

        if has_cgi:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type},
                keep_blank_values=True
            )
            if 'file_upload' not in form:
                return (False, "No file_upload field in form")

            file_items: list[Any] = form['file_upload']
            # Handle single item case (FieldStorage returns single item if one file)
            if not isinstance(file_items, list):  # type: ignore
                file_items = [file_items]

            uploaded_files: list[str] = []
            for item in file_items:
                if item.filename:
                    fn = os.path.basename(item.filename)

                    # Prevent uploads overwriting protected files
                    if fn in PROTECTED_FILES:
                        return (False, "Cannot overwrite protected file: %s" % fn)

                    dest_path = os.path.join(DIRECTORY, fn)
                    tmpname = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=DIRECTORY, delete=False) as tmpf:
                            shutil.copyfileobj(item.file, tmpf)
                            tmpname = tmpf.name
                        os.replace(tmpname, dest_path)
                        uploaded_files.append(fn)
                    except IOError:
                        try:
                            if tmpname:
                                os.unlink(tmpname)
                        except Exception:
                            pass
                        return (False, "Can't create file: %s" % fn)

            if uploaded_files:
                return (True, "Files uploaded: %s" % ", ".join(uploaded_files))
            else:
                return (False, "No files uploaded")

        boundary = None
        parts = content_type.split(';')
        for p in parts:
            p = p.strip()
            if p.startswith('boundary='):
                boundary = p.split('=', 1)[1]
                if boundary.startswith('"') and boundary.endswith('"'):
                    boundary = boundary[1:-1]
                break
        if not boundary:
            return (False, "No boundary in Content-Type")

        try:
            body = self.rfile.read(content_length)
        except Exception as e:
            return (False, f"Failed to read request body: {e}")

        b_boundary = b'--' + boundary.encode('utf-8')
        raw_parts = body.split(b_boundary)
        uploaded_files: list[str] = []

        for raw in raw_parts:
            if not raw:
                continue
            if raw.startswith(b'\r\n'):
                raw = raw[2:]
            if raw.endswith(b'--\r\n') or raw.endswith(b'--'):
                raw = raw.rstrip(b'-\r\n')
            if not raw:
                continue

            try:
                header_blob, part_body = raw.split(b'\r\n\r\n', 1)
            except ValueError:
                continue
            if part_body.endswith(b'\r\n'):
                part_body = part_body[:-2]

            header_lines = header_blob.decode('utf-8', errors='ignore').split('\r\n')
            headers: dict[str, str] = {}
            for hl in header_lines:
                if ':' in hl:
                    k, v = hl.split(':', 1)
                    headers[k.strip().lower()] = v.strip()

            cd = headers.get('content-disposition', '')
            if 'filename=' in cd:
                fn = None
                for part in cd.split(';'):
                    part = part.strip()
                    if part.startswith('filename='):
                        val = part.split('=', 1)[1].strip()
                        if val.startswith('"') and val.endswith('"'):
                            val = val[1:-1]
                        fn = os.path.basename(val)
                        break
                if not fn:
                    continue
                dest_path = os.path.join(DIRECTORY, fn)
                tmpname = None
                try:
                    with tempfile.NamedTemporaryFile(dir=DIRECTORY, delete=False) as tmpf:
                        tmpf.write(part_body)
                        tmpname = tmpf.name
                    os.replace(tmpname, dest_path)
                    uploaded_files.append(fn)
                except Exception:
                    try:
                        if tmpname:
                            os.unlink(tmpname)
                    except Exception:
                        pass
                    return (False, f"Can't create file: {fn}")

        if uploaded_files:
            return (True, "Files uploaded: %s" % ", ".join(uploaded_files))
        else:
            return (False, "No files uploaded")

    def list_directory(self, path: str):  # type: ignore
        """Serve the list of files in the directory with a modern UI."""
        try:
            file_list = os.listdir(path)
        except os.error:
            self.send_error(404, "No permission to list directory")
            return None
        
        file_list.sort(key=lambda a: a.lower())
        
        # Generator for file data
        files_data = []
        for name in file_list:
            if name in PROTECTED_FILES:
                continue
            fullname = os.path.join(path, name)
            displayname = linkname = name
            is_dir = os.path.isdir(fullname)
            if is_dir:
                displayname = name + "/"
                linkname = name + "/"
            
            size_str = "-"
            if not is_dir:
                try:
                    size = os.path.getsize(fullname)
                    size_str = self.format_file_size(size)
                except:
                    size_str = "?"
            
            files_data.append({
                "name": name,
                "displayname": displayname,
                "linkname": linkname,
                "size": size_str,
                "is_dir": is_dir
            })

        # HTML Template
        html_template = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Nas Server</title>
    <style>
        :root {
            --bg-color: #f8f9fa;
            --card-bg: #ffffff;
            --text-main: #2d3436;
            --text-secondary: #636e72;
            --accent: #0984e3;
            --accent-hover: #74b9ff;
            --border: #dfe6e9;
            --danger: #d63031;
            --success: #00b894;
            --header-bg: #ffffff;
            --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }

        [data-theme="dark"] {
            --bg-color: #1e1e1e;
            --card-bg: #2d2d2d;
            --text-main: #dfe6e9;
            --text-secondary: #b2bec3;
            --accent: #74b9ff;
            --accent-hover: #0984e3;
            --border: #444;
            --header-bg: #2d2d2d;
            --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.3), 0 2px 4px -1px rgba(0, 0, 0, 0.2);
        }

        body {
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            margin: 0;
            padding: 0;
            transition: background-color 0.3s, color 0.3s;
        }

        /* Layout */
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }

        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1rem 0;
            margin-bottom: 2rem;
            border-bottom: 1px solid var(--border);
        }

        h1 { margin: 0; font-size: 1.5rem; font-weight: 700; color: var(--accent); }
        
        .toolbar {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }

        /* Controls */
        input[type="text"] {
            padding: 10px 15px;
            border-radius: 8px;
            border: 1px solid var(--border);
            background: var(--card-bg);
            color: var(--text-main);
            width: 250px;
            outline: none;
            transition: all 0.2s;
        }
        input[type="text"]:focus {
            border-color: var(--accent);
            box-shadow: 0 0 0 3px rgba(9, 132, 227, 0.1);
        }

        .btn {
            padding: 10px 16px;
            border-radius: 8px;
            border: none;
            background: var(--card-bg);
            color: var(--text-main);
            cursor: pointer;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        .btn:hover { transform: translateY(-1px); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .btn-primary { background: var(--accent); color: white; }
        .btn-primary:hover { background: var(--accent-hover); }

        /* Upload Area */
        #drop-zone {
            border: 2px dashed var(--border);
            border-radius: 12px;
            padding: 40px;
            text-align: center;
            background: var(--card-bg);
            margin-bottom: 30px;
            transition: all 0.3s;
            cursor: pointer;
        }
        #drop-zone.dragover {
            border-color: var(--accent);
            background: rgba(9, 132, 227, 0.05);
        }
        #fileInput { display: none; }

        /* File List */
        .file-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
            gap: 20px;
        }
        
        .file-list-view .file-grid {
            grid-template-columns: 1fr;
            gap: 10px;
        }

        .file-card {
            background: var(--card-bg);
            border-radius: 10px;
            padding: 15px;
            box-shadow: var(--shadow);
            transition: transform 0.2s;
            display: flex;
            flex-direction: column;
            align-items: center;
            text-align: center;
            position: relative;
            text-decoration: none;
            color: var(--text-main);
            border: 1px solid transparent;
        }
        
        .file-card:hover {
            transform: translateY(-4px);
            border-color: var(--accent);
        }

        .file-list-view .file-card {
            flex-direction: row;
            padding: 10px 20px;
            text-align: left;
            align-items: center;
        }
        .file-list-view .file-card:hover { transform: translateX(4px); }

        .file-icon {
            width: 48px;
            height: 48px;
            margin-bottom: 10px;
            color: var(--text-secondary);
        }
        .file-list-view .file-icon { margin-bottom: 0; margin-right: 15px; width: 32px; height: 32px; }

        .file-info { flex: 1; min-width: 0; }
        .file-name {
            font-weight: 500;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            display: block;
            margin-bottom: 4px;
        }
        .file-meta { font-size: 0.8rem; color: var(--text-secondary); }

        .file-actions {
            opacity: 0;
            transition: opacity 0.2s;
            display: flex;
            gap: 5px;
        }
        .file-card:hover .file-actions { opacity: 1; }
        
        .action-btn {
            background: none;
            border: none;
            padding: 6px;
            border-radius: 50%;
            cursor: pointer;
            color: var(--text-secondary);
            transition: background 0.2s;
        }
        .action-btn:hover { background: rgba(0,0,0,0.1); color: var(--accent); }
        .action-btn.delete:hover { color: var(--danger); }

        /* Progress Bar */
        #progress-container {
            position: fixed;
            bottom: 20px;
            right: 20px;
            width: 300px;
            background: var(--card-bg);
            padding: 15px;
            border-radius: 10px;
            box-shadow: var(--shadow);
            z-index: 1000;
            display: none;
        }
        .progress-bar {
            height: 6px;
            background: var(--border);
            border-radius: 3px;
            overflow: hidden;
            margin-top: 10px;
        }
        .progress-fill {
            height: 100%;
            background: var(--accent);
            width: 0%;
            transition: width 0.3s;
        }

        /* SVGs */
        .icon { width: 24px; height: 24px; stroke: currentColor; fill: none; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
    </style>
</head>
<body>

<div class="container">
    <header>
        <div style="display: flex; align-items: center; gap: 10px;">
            <svg class="icon" style="width:32px; height:32px; color:var(--accent);" viewBox="0 0 24 24"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"></path><polyline points="9 22 9 12 15 12 15 22"></polyline></svg>
            <h1>NAS Server</h1>
        </div>
        <div class="toolbar">
            <input type="text" id="search" placeholder="Search files...">
            <button class="btn" onclick="toggleLayout()">
                <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect></svg>
                View
            </button>
            <button class="btn" onclick="toggleTheme()">
                <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>
                Theme
            </button>
        </div>
    </header>

    <div id="drop-zone" onclick="document.getElementById('fileInput').click()">
        <svg class="icon" style="width: 48px; height: 48px; color: var(--accent); margin-bottom: 10px;" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>
        <h3>Drag & Drop files here or click to upload</h3>
        <p style="color: var(--text-secondary);">Max file size: Unlimited</p>
        <input type="file" id="fileInput" name="file_upload" multiple onchange="handleFiles(this.files)">
    </div>

    <div id="file-container" class="file-grid">
        <!-- File Loop -->
        {% for file in files %}
        <div class="file-card" data-name="{{ file.name }}">
            <a href="{{ file.linkname }}" class="file-icon" style="text-decoration: none; color: inherit; width: 100%; display: flex; justify-content: center;">
                {% if file.is_dir %}
                <svg class="icon" style="width: 48px; height: 48px;" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>
                {% else %}
                <svg class="icon" style="width: 48px; height: 48px;" viewBox="0 0 24 24"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><polyline points="13 2 13 9 20 9"></polyline></svg>
                {% endif %}
            </a>
            <div class="file-info" style="width: 100%;">
                <a href="{{ file.linkname }}" class="file-name" style="text-decoration: none; color: inherit;">{{ file.displayname }}</a>
                <div class="file-meta">{{ file.size }}</div>
            </div>
            <div class="file-actions">
                <a href="/download?file={{ file.linkname }}" class="action-btn" title="Download" download>
                    <svg class="icon" style="width: 16px; height: 16px;" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                </a>
                <button class="action-btn delete" title="Delete" onclick="deleteFile('{{ file.name }}')">
                    <svg class="icon" style="width: 16px; height: 16px;" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                </button>
            </div>
        </div>
        {% endfor %}
    </div>
</div>

<div id="progress-container">
    <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
        <strong id="progress-status">Uploading...</strong>
        <span id="progress-percent">0%</span>
    </div>
    <div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div>
</div>

<script>
    // Theme Management
    function toggleTheme() {
        const current = document.documentElement.getAttribute('data-theme');
        const next = current === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
    }
    
    // Initialize Theme
    const savedTheme = localStorage.getItem('theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', savedTheme);

    // Layout Management
    function toggleLayout() {
        const container = document.getElementById('file-container');
        container.classList.toggle('file-list-view');
        localStorage.setItem('layout', container.classList.contains('file-list-view') ? 'list' : 'grid');
    }

    // Initialize Layout
    if (localStorage.getItem('layout') === 'list') {
        document.getElementById('file-container').classList.add('file-list-view');
    }

    // Search
    document.getElementById('search').addEventListener('input', (e) => {
        const term = e.target.value.toLowerCase();
        document.querySelectorAll('.file-card').forEach(card => {
            const name = card.getAttribute('data-name').toLowerCase();
            card.style.display = name.includes(term) ? 'flex' : 'none';
        });
    });

    // Drag and Drop
    const dropZone = document.getElementById('drop-zone');
    
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, preventDefaults, false);
        document.body.addEventListener(eventName, preventDefaults, false);
    });

    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }

    ['dragenter', 'dragover'].forEach(eventName => {
        dropZone.addEventListener(eventName, highlight, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, unhighlight, false);
    });

    function highlight(e) { dropZone.classList.add('dragover'); }
    function unhighlight(e) { dropZone.classList.remove('dragover'); }

    dropZone.addEventListener('drop', handleDrop, false);

    function handleDrop(e) {
        const dt = e.dataTransfer;
        const files = dt.files;
        handleFiles(files);
    }

    function handleFiles(files) {
        if (!files.length) return;
        
        const formData = new FormData();
        for (let i = 0; i < files.length; i++) {
            formData.append('file_upload', files[i]);
        }

        uploadFiles(formData);
    }

    function uploadFiles(formData) {
        const pContainer = document.getElementById('progress-container');
        const pFill = document.getElementById('progress-fill');
        const pText = document.getElementById('progress-percent');
        const pStatus = document.getElementById('progress-status');

        pContainer.style.display = 'block';
        pStatus.textContent = 'Uploading...';
        
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/', true);

        xhr.upload.onprogress = function(e) {
            if (e.lengthComputable) {
                const percentComplete = (e.loaded / e.total) * 100;
                pFill.style.width = percentComplete + '%';
                pText.textContent = Math.round(percentComplete) + '%';
            }
        };

        xhr.onload = function() {
            if (this.status == 200 || this.status == 303) {
                pStatus.textContent = 'Done!';
                pFill.style.background = 'var(--success)';
                pFill.style.width = '100%';
                setTimeout(() => window.location.reload(), 1000);
            } else {
                pStatus.textContent = 'Error!';
                pFill.style.background = 'var(--danger)';
            }
        };

        xhr.send(formData);
    }

    // Delete
    function deleteFile(filename) {
        if (!confirm('Are you sure you want to delete ' + filename + '?')) return;
        
        const pContainer = document.getElementById('progress-container');
        const pStatus = document.getElementById('progress-status');
        const pFill = document.getElementById('progress-fill');
        
        pContainer.style.display = 'block';
        pStatus.textContent = 'Requesting delete...';
        pFill.style.width = '50%';
        
        fetch('/delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({file: filename, action: 'request'})
        })
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                pStatus.textContent = 'Error: ' + data.error;
                pFill.style.background = 'var(--danger)';
                setTimeout(() => pContainer.style.display = 'none', 3000);
            } else {
                pStatus.textContent = 'Deleted!';
                pFill.style.width = '100%';
                pFill.style.background = 'var(--success)';
                setTimeout(() => window.location.reload(), 1000);
            }
        });
    }
</script>
</body>
</html>
"""
        
        # Render Template (Simple string replacement/jinja-like manual loop)
        file_cards_html = []
        for file in files_data:
            icon_svg = ""
            if file['is_dir']:
                icon_svg = '<svg class="icon" style="width: 48px; height: 48px;" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>'
            else:
                # Basic file icon, could be enhanced with extension check like before
                ext = os.path.splitext(file['name'])[1].lower()
                if ext in ['.png', '.jpg', '.jpeg', '.gif']:
                    icon_svg = '<svg class="icon" style="width: 48px; height: 48px;" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>'
                elif ext in ['.mp3', '.wav']:
                    icon_svg = '<svg class="icon" style="width: 48px; height: 48px;" viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>'
                elif ext in ['.mp4', '.mov']:
                    icon_svg = '<svg class="icon" style="width: 48px; height: 48px;" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18"></rect><line x1="7" y1="2" x2="7" y2="22"></line><line x1="17" y1="2" x2="17" y2="22"></line><line x1="2" y1="12" x2="22" y2="12"></line></svg>'
                else:
                    icon_svg = '<svg class="icon" style="width: 48px; height: 48px;" viewBox="0 0 24 24"><path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"></path><polyline points="13 2 13 9 20 9"></polyline></svg>'

            file_cards_html.append(f"""
            <div class="file-card" data-name="{file['name']}">
                <a href="{file['linkname']}" class="file-icon" style="text-decoration: none; color: inherit; width: 100%; display: flex; justify-content: center;">
                    {icon_svg}
                </a>
                <div class="file-info" style="width: 100%;">
                    <a href="{file['linkname']}" class="file-name" style="text-decoration: none; color: inherit;">{file['displayname']}</a>
                    <div class="file-meta">{file['size']}</div>
                </div>
                <div class="file-actions">
                    <a href="/download?file={urllib.parse.quote(file['name'])}" class="action-btn" title="Download" download>
                        <svg class="icon" style="width: 16px; height: 16px;" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
                    </a>
                    <button class="action-btn delete" title="Delete" onclick="deleteFile('{file['name']}')">
                        <svg class="icon" style="width: 16px; height: 16px;" viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                    </button>
                </div>
            </div>
            """)
        
        # Inject content
        try:
            split1 = html_template.split('{% for file in files %}', 1)
            split2 = split1[1].split('{% endfor %}', 1)
            final_html = split1[0] + "".join(file_cards_html) + split2[1]
        except Exception:
            final_html = "<h1>Template Error</h1>"

        encoded = final_html.encode('utf-8')
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        return None

    def format_file_size(self, size: float) -> str:
        """Format file size in human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

if __name__ == '__main__':
    handler = CustomRequestHandler
    os.chdir(DIRECTORY)
    import socket

    class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    bind_addr = "0.0.0.0" 

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        local_ip = "127.0.0.1"

    try:
        with ThreadingTCPServer((bind_addr, PORT), handler) as httpd:
            print(f"Serving at {bind_addr}:{PORT} (dir: {DIRECTORY})")
            print(f"Accessible locally: http://localhost:{PORT}")
            print(f"Accessible on LAN:   http://{local_ip}:{PORT}")
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    except Exception as e:
        print(f"An error occurred: {e}")
