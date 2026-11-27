import { useState, useEffect } from 'react';
import {
  listKnowledge,
  getKnowledge,
  uploadKnowledge,
  deleteKnowledge,
} from '../api/adminApi';
import './Knowledge.css';

function Knowledge() {
  const [documents, setDocuments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);
  const [selectedFile, setSelectedFile] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [selectedDoc, setSelectedDoc] = useState(null);

  useEffect(() => {
    fetchDocuments();
  }, []);

  const flashSuccess = (msg) => {
    setSuccess(msg);
    setTimeout(() => setSuccess(null), 3000);
  };

  const fetchDocuments = async () => {
    try {
      setLoading(true);
      const res = await listKnowledge();
      setDocuments(res?.documents || []);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleUpload = async (e) => {
    e.preventDefault();
    if (!selectedFile) {
      setError('Please select a file (.pdf, .md, .markdown, .txt)');
      return;
    }
    try {
      setUploading(true);
      const res = await uploadKnowledge(selectedFile);
      setSelectedFile(null);
      setError(null);
      flashSuccess(`Uploaded ${res?.chunks ?? 1} chunk(s)`);
      fetchDocuments();
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
    }
  };

  const handleViewDoc = async (docId) => {
    try {
      const doc = await getKnowledge(docId);
      setSelectedDoc(doc);
      setError(null);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleDeleteDoc = async (docId) => {
    if (!window.confirm('Delete this knowledge document?')) return;
    try {
      await deleteKnowledge(docId);
      if (selectedDoc?.id === docId) setSelectedDoc(null);
      setError(null);
      flashSuccess('Document deleted');
      fetchDocuments();
    } catch (err) {
      setError(err.message);
    }
  };

  if (loading) {
    return <div className="admin-loading">Loading knowledge base...</div>;
  }

  return (
    <div className="knowledge-page">
      <div className="page-header">
        <h1>Knowledge Base</h1>
      </div>

      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      <form className="admin-form-card inline-form" onSubmit={handleUpload}>
        <label className="file-label">
          Upload Document (.pdf, .md, .markdown, .txt)
          <input
            type="file"
            accept=".pdf,.md,.markdown,.txt"
            onChange={(e) => setSelectedFile(e.target.files?.[0] || null)}
          />
        </label>
        <button type="submit" className="btn-primary" disabled={uploading}>
          {uploading ? 'Uploading...' : 'Upload'}
        </button>
      </form>

      <div className="admin-table-card">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Source</th>
              <th>Preview</th>
              <th>Created At</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {documents.length === 0 ? (
              <tr>
                <td colSpan="5" className="empty-cell">No knowledge documents uploaded</td>
              </tr>
            ) : (
              documents.map((d) => (
                <tr key={d.id}>
                  <td>{d.id}</td>
                  <td>{d.source || 'upload'}</td>
                  <td className="preview-cell">
                    {(d.content || '').slice(0, 100)}
                    {(d.content || '').length > 100 ? '...' : ''}
                  </td>
                  <td>{d.created_at || '-'}</td>
                  <td className="actions-cell">
                    <button type="button" className="btn-sm" onClick={() => handleViewDoc(d.id)}>
                      View
                    </button>
                    <button type="button" className="btn-sm btn-danger" onClick={() => handleDeleteDoc(d.id)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {selectedDoc && (
        <div className="modal-backdrop" role="dialog" aria-label="Knowledge Document Detail">
          <div className="modal-card">
            <div className="modal-header">
              <h2>Document #{selectedDoc.id}: {selectedDoc.source || 'upload'}</h2>
              <button type="button" className="btn-sm" onClick={() => setSelectedDoc(null)}>
                Close
              </button>
            </div>
            <pre className="json-preview">{selectedDoc.content}</pre>
            {selectedDoc.provenance && (
              <pre className="json-preview">{JSON.stringify(selectedDoc.provenance, null, 2)}</pre>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export default Knowledge;
