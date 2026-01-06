import http.server
import socketserver
import os
import posixpath
import urllib.parse
import shutil
import html
import tempfile
import time
import json
import uuid

try:
    import cgi
    _HAS_CGI = True
except Exception:
    cgi = None
    _HAS_CGI = False

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

pending_deletes = {}

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
            except:
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

    def _handle_delete_request(self, filename):
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

    def _handle_delete_confirm(self, filename, token):
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

    def _execute_delete(self, filename, filepath, token):
        """Actually delete the file."""
        try:
            if os.path.isfile(filepath):
                os.remove(filepath)
        except Exception as e:
            print(f"Error deleting {filename}: {e}")

    def send_json_response(self, status_code, data):
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

    def deal_post_data(self):
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

        if _HAS_CGI:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': content_type},
                keep_blank_values=True
            )
            if 'file_upload' not in form:
                return (False, "No file_upload field in form")

            file_items = form['file_upload']
            if not isinstance(file_items, list):
                file_items = [file_items]

            uploaded_files = []
            for item in file_items:
                if item.filename:
                    fn = os.path.basename(item.filename)

                    # Prevent uploads overwriting protected files
                    if fn in PROTECTED_FILES:
                        return (False, "Cannot overwrite protected file: %s" % fn)

                    dest_path = os.path.join(DIRECTORY, fn)
                    try:
                        with tempfile.NamedTemporaryFile(dir=DIRECTORY, delete=False) as tmpf:
                            shutil.copyfileobj(item.file, tmpf)
                            tmpname = tmpf.name
                        os.replace(tmpname, dest_path)
                        uploaded_files.append(fn)
                    except IOError:
                        try:
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
        uploaded_files = []

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
            headers = {}
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
                try:
                    with tempfile.NamedTemporaryFile(dir=DIRECTORY, delete=False) as tmpf:
                        tmpf.write(part_body)
                        tmpname = tmpf.name
                    os.replace(tmpname, dest_path)
                    uploaded_files.append(fn)
                except Exception:
                    try:
                        os.unlink(tmpname)
                    except Exception:
                        pass
                    return (False, f"Can't create file: {fn}")

        if uploaded_files:
            return (True, "Files uploaded: %s" % ", ".join(uploaded_files))
        else:
            return (False, "No files uploaded")

    def list_directory(self, path):
        """Serve the list of files in the directory with an upload form and progress bar."""
        try:
            list = os.listdir(path)
        except os.error:
            self.send_error(404, "No permission to list directory")
            return None
        list.sort(key=lambda a: a.lower())
        
        r = []
        r.append('<!DOCTYPE html>')
        r.append('<html><head><title>Python NAS Server</title>')
        r.append('<style>')
        r.append('body { font-family: Arial, sans-serif; margin: 20px; }')
        r.append('h1 { color: #333; }')
        r.append('h2 { color: #666; margin-top: 30px; }')
        r.append('.upload-section { background: #f5f5f5; padding: 20px; border-radius: 8px; }')
        r.append('.file-list { list-style-type: none; padding: 0; }')
        r.append('.file-item { background: white; padding: 10px; margin: 5px 0; border-radius: 5px; display: flex; justify-content: space-between; align-items: center; border: 1px solid #ddd; }')
        r.append('.file-name { flex: 1; }')
        r.append('.file-size { color: #888; margin-right: 15px; font-size: 0.9em; }')
        r.append('.btn { padding: 6px 12px; margin-left: 5px; text-decoration: none; border-radius: 4px; border: none; cursor: pointer; font-size: 0.9em; }')
        r.append('.btn-download { background: #4CAF50; color: white; }')
        r.append('.btn-download:hover { background: #45a049; }')
        r.append('.btn-delete { background: #f44336; color: white; }')
        r.append('.btn-delete:hover { background: #da190b; }')
        r.append('.btn-upload { background: #2196F3; color: white; }')
        r.append('.btn-upload:hover { background: #0b7dda; }')
        r.append('#progressContainer { display: none; margin-top: 20px; }')
        r.append('.progress-bar { width: 100%; height: 25px; background: #ddd; border-radius: 4px; overflow: hidden; }')
        r.append('.progress-fill { height: 100%; background: linear-gradient(90deg, #4CAF50, #45a049); width: 0%; transition: width 0.3s; display: flex; align-items: center; justify-content: center; color: white; font-weight: bold; font-size: 0.85em; }')
        r.append('.progress-info { margin-top: 10px; font-size: 0.9em; color: #666; }')
        r.append('</style>')
        r.append('</head>')
        r.append('<body><h1>Python NAS Server - File Manager</h1>')
        
        r.append('<div class="upload-section">')
        r.append('<h2>Upload Files</h2>')
        r.append('<form id="uploadForm" enctype="multipart/form-data" method="post">')
        r.append('<input type="file" id="fileInput" name="file_upload" multiple>')
        r.append('<button type="submit" class="btn btn-upload">Upload</button>')
        r.append('</form>')
        r.append('<div id="progressContainer">')
        r.append('<div class="progress-bar"><div class="progress-fill" id="progressFill">0%</div></div>')
        r.append('<div class="progress-info">')
        r.append('<span id="uploadStatus">Uploading...</span><br>')
        r.append('<span id="uploadSpeed">Speed: 0 KB/s</span> | <span id="uploadTime">Time: 0s</span>')
        r.append('</div>')
        r.append('</div>')
        r.append('</div>')
        r.append('<hr>')
        
        r.append('<h2>Files</h2>')
        r.append('<ul class="file-list">')
        for name in list:
            if name in PROTECTED_FILES:
                continue
            fullname = os.path.join(path, name)
            displayname = linkname = name
            if os.path.isdir(fullname):
                displayname = name + "/"
                linkname = name + "/"
                r.append('<li class="file-item">')
                r.append('<span class="file-name"><a href="%s">📁 %s</a></span>' % 
                        (urllib.parse.quote(linkname), html.escape(displayname)))
                r.append('</li>')
            else:
                try:
                    fsize = os.path.getsize(fullname)
                    fsize_str = self.format_file_size(fsize)
                except:
                    fsize_str = "?"
                
                if os.path.islink(fullname):
                    displayname = name + "@"
                
                dl_url = "/download?file=" + urllib.parse.quote(linkname)
                r.append('<li class="file-item">')
                r.append('<span class="file-name">📄 %s</span>' % html.escape(displayname))
                r.append('<span class="file-size">%s</span>' % fsize_str)
                r.append('<a href="%s" class="btn btn-download">Download</a>' % dl_url)
                r.append('<button class="btn btn-delete" onclick="deleteFile(\'%s\')">Delete</button>' % html.escape(name))
                r.append('</li>')
        
        r.append('</ul>')
        r.append('<script>')
        r.append('''
function deleteFile(filename) {
    if (!confirm('Are you sure you want to delete ' + filename + '?')) {
        return;
    }
    
    const progressContainer = document.getElementById('progressContainer');
    const progressFill = document.getElementById('progressFill');
    const uploadStatus = document.getElementById('uploadStatus');
    const uploadSpeed = document.getElementById('uploadSpeed');
    const uploadTime = document.getElementById('uploadTime');
    
    progressContainer.style.display = 'block';
    uploadStatus.textContent = 'Requesting delete confirmation from server...';
    progressFill.style.width = '50%';
    progressFill.textContent = '...';
    
    fetch('/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({file: filename, action: 'request'})
    })
    .then(response => response.json())
    .then(data => {
        if (data.error) {
            uploadStatus.textContent = 'Delete denied: ' + data.error;
            progressFill.style.width = '100%';
            progressFill.textContent = '✗';
            setTimeout(() => progressContainer.style.display = 'none', 3000);
        } else {
            uploadStatus.textContent = 'File deleted! Reloading...';
            progressFill.style.width = '100%';
            progressFill.textContent = '✓';
            setTimeout(() => window.location.reload(), 1500);
        }
    })
    .catch(err => {
        uploadStatus.textContent = 'Error: ' + err;
        progressFill.style.width = '100%';
        progressFill.textContent = '✗';
        setTimeout(() => progressContainer.style.display = 'none', 3000);
    });
}

document.getElementById('uploadForm').addEventListener('submit', function(e) {
    e.preventDefault();
    const fileInput = document.getElementById('fileInput');
    const files = fileInput.files;
    
    if (files.length === 0) {
        alert('Please select files to upload');
        return;
    }
    
    const formData = new FormData();
    let totalSize = 0;
    for (let file of files) {
        formData.append('file_upload', file);
        totalSize += file.size;
    }
    
    const progressContainer = document.getElementById('progressContainer');
    const progressFill = document.getElementById('progressFill');
    const uploadStatus = document.getElementById('uploadStatus');
    const uploadSpeed = document.getElementById('uploadSpeed');
    const uploadTime = document.getElementById('uploadTime');
    
    progressContainer.style.display = 'block';
    const startTime = Date.now();
    
    const xhr = new XMLHttpRequest();
    
    xhr.upload.addEventListener('progress', function(e) {
        if (e.lengthComputable) {
            const percent = (e.loaded / e.total) * 100;
            progressFill.style.width = percent + '%';
            progressFill.textContent = Math.round(percent) + '%';
            
            const elapsedSeconds = (Date.now() - startTime) / 1000;
            const speedBytesPerSec = e.loaded / elapsedSeconds;
            const speedKBPerSec = (speedBytesPerSec / 1024).toFixed(2);
            
            uploadStatus.textContent = 'Uploading: ' + e.loaded.toLocaleString() + ' / ' + e.total.toLocaleString() + ' bytes';
            uploadSpeed.textContent = 'Speed: ' + speedKBPerSec + ' KB/s';
            uploadTime.textContent = 'Time: ' + Math.round(elapsedSeconds) + 's';
        }
    });
    
    xhr.addEventListener('load', function() {
        if (xhr.status === 303 || xhr.status === 200) {
            uploadStatus.textContent = 'Upload complete! Redirecting...';
            setTimeout(() => window.location.reload(), 1500);
        } else {
            uploadStatus.textContent = 'Upload failed!';
        }
    });
    
    xhr.addEventListener('error', function() {
        uploadStatus.textContent = 'Upload error!';
    });
    
    xhr.open('POST', '/', true);
    xhr.send(formData);
});

// Download progress tracking
document.addEventListener('click', function(e) {
    if (e.target.classList.contains('btn-download')) {
        e.preventDefault();
        const url = e.target.href;
        const filename = new URLSearchParams(new URL(url, window.location).search).get('file');
        
        const progressContainer = document.getElementById('progressContainer');
        const progressFill = document.getElementById('progressFill');
        const uploadStatus = document.getElementById('uploadStatus');
        const uploadSpeed = document.getElementById('uploadSpeed');
        const uploadTime = document.getElementById('uploadTime');
        
        progressContainer.style.display = 'block';
        const startTime = Date.now();
        
        fetch(url)
            .then(response => {
                const contentLength = response.headers.get('content-length');
                const total = parseInt(contentLength, 10);
                const reader = response.body.getReader();
                let loaded = 0;
                
                const chunks = [];
                
                function pump() {
                    return reader.read().then(({ done, value }) => {
                        if (done) {
                            // Create blob and trigger download
                            const blob = new Blob(chunks);
                            const downloadUrl = window.URL.createObjectURL(blob);
                            const a = document.createElement('a');
                            a.href = downloadUrl;
                            a.download = filename;
                            document.body.appendChild(a);
                            a.click();
                            window.URL.revokeObjectURL(downloadUrl);
                            document.body.removeChild(a);
                            
                            uploadStatus.textContent = 'Download complete!';
                            setTimeout(() => {
                                progressContainer.style.display = 'none';
                            }, 2000);
                            return;
                        }
                        
                        chunks.push(value);
                        loaded += value.length;
                        
                        const percent = (loaded / total) * 100;
                        progressFill.style.width = percent + '%';
                        progressFill.textContent = Math.round(percent) + '%';
                        
                        const elapsedSeconds = (Date.now() - startTime) / 1000;
                        const speedBytesPerSec = loaded / elapsedSeconds;
                        const speedKBPerSec = (speedBytesPerSec / 1024).toFixed(2);
                        
                        uploadStatus.textContent = 'Downloading: ' + loaded.toLocaleString() + ' / ' + total.toLocaleString() + ' bytes';
                        uploadSpeed.textContent = 'Speed: ' + speedKBPerSec + ' KB/s';
                        uploadTime.textContent = 'Time: ' + Math.round(elapsedSeconds) + 's';
                        
                        return pump();
                    });
                }
                
                return pump();
            })
            .catch(err => {
                uploadStatus.textContent = 'Download failed!';
                console.error(err);
            });
    }
});
        ''')
        r.append('</script>')
        
        r.append('</body></html>')
        
        encoded = '\n'.join(r).encode('utf-8')
        self.send_response(200)
        self.send_header("Content-type", "text/html; charset=%s" % 'utf-8')
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        return None

    def format_file_size(self, size):
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

