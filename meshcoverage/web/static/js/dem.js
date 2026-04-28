// DEM files management JavaScript

document.addEventListener('DOMContentLoaded', function() {
    loadDemFiles();
    setupUploadForm();
    setupDeleteModal();
});

function loadDemFiles() {
    fetch('/api/dem')
        .then(response => response.json())
        .then(data => {
            const listDiv = document.getElementById('dem-files-list');
            if (data.files && data.files.length > 0) {
                let html = '<table class="dem-files-table"><thead><tr><th>Thumbnail</th><th>Filename</th><th>Size</th><th>Actions</th></tr></thead><tbody>';
                data.files.forEach(file => {
                    html += `<tr>
                        <td><img data-src="/api/dem/thumbnail/${file.name}?size=80" alt="${file.name}" class="dem-thumbnail lazy-thumbnail" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='80' height='80'%3E%3Crect fill='%23e5e7eb' width='80' height='80'/%3E%3Ctext x='50%25' y='50%25' text-anchor='middle' dy='.3em' font-size='12' fill='%239ca3af'%3ELoading...%3C/text%3E%3C/svg%3E"></td>
                        <td>${file.name}</td>
                        <td>${formatBytes(file.size)}</td>
                        <td>
                            <a href="/api/dem/download/${file.name}" class="btn btn-ghost btn-sm">Download</a>
                            <button class="btn btn-danger btn-sm" onclick="confirmDelete('${file.name}')">Delete</button>
                        </td>
                    </tr>`;
                });
                html += '</tbody></table>';
                listDiv.innerHTML = html;
                setupLazyLoading();
            } else {
                listDiv.innerHTML = '<p class="empty-msg">No DEM files found.</p>';
            }
        })
        .catch(error => {
            console.error('Error loading DEM files:', error);
            document.getElementById('dem-files-list').innerHTML = '<p class="error-msg">Error loading DEM files.</p>';
        });
}

function setupLazyLoading() {
    const images = document.querySelectorAll('.lazy-thumbnail');
    
    const imageObserver = new IntersectionObserver((entries, observer) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                const img = entry.target;
                img.src = img.dataset.src;
                img.classList.remove('lazy-thumbnail');
                observer.unobserve(img);
            }
        });
    }, {
        rootMargin: '50px'
    });
    
    images.forEach(img => imageObserver.observe(img));
}


function setupUploadForm() {
    const form = document.getElementById('upload-form');
    form.addEventListener('submit', function(e) {
        e.preventDefault();
        const fileInput = document.getElementById('dem-file');
        const file = fileInput.files[0];
        if (!file) return;

        const formData = new FormData();
        formData.append('file', file);

        const progressWrap = document.getElementById('upload-progress');
        const progressBar = document.getElementById('upload-progress-bar');
        const progressLabel = document.getElementById('upload-progress-label');

        progressWrap.classList.remove('hidden');

        fetch('/api/dem/upload', {
            method: 'POST',
            body: formData
        })
        .then(response => {
            if (response.ok) {
                showToast('DEM file uploaded successfully', 'success');
                loadDemFiles();
                fileInput.value = '';
            } else {
                throw new Error('Upload failed');
            }
        })
        .catch(error => {
            console.error('Upload error:', error);
            showToast('Upload failed', 'error');
        })
        .finally(() => {
            progressWrap.classList.add('hidden');
        });
    });
}

function confirmDelete(filename) {
    document.getElementById('delete-filename').textContent = filename;
    document.getElementById('delete-modal').classList.remove('hidden');
}

function setupDeleteModal() {
    document.getElementById('btn-cancel-delete').addEventListener('click', () => {
        document.getElementById('delete-modal').classList.add('hidden');
    });

    document.getElementById('btn-confirm-delete').addEventListener('click', () => {
        const filename = document.getElementById('delete-filename').textContent;
        deleteDemFile(filename);
        document.getElementById('delete-modal').classList.add('hidden');
    });
}

function deleteDemFile(filename) {
    fetch(`/api/dem/delete/${filename}`, {
        method: 'DELETE'
    })
    .then(response => {
        if (response.ok) {
            showToast('DEM file deleted successfully', 'success');
            loadDemFiles();
        } else {
            throw new Error('Delete failed');
        }
    })
    .catch(error => {
        console.error('Delete error:', error);
        showToast('Delete failed', 'error');
    });
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function showToast(message, type) {
    // Assuming there's a toast system, similar to other pages
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.textContent = message;
    document.getElementById('toast-container').appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
}