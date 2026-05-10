import { FileSpreadsheet, Loader2, Upload } from "lucide-react";

export default function UploadPanel({ fileName, onUpload, loading, error }) {
  return (
    <section className="panel upload-panel">
      <div className="panel-title">
        <FileSpreadsheet size={18} />
        <span>Dataset</span>
      </div>
      <label className={`drop-zone ${loading ? "is-loading" : ""}`}>
        <input
          type="file"
          accept=".csv,.xlsx,.xls"
          disabled={loading}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) onUpload(file);
          }}
        />
        {loading ? <Loader2 className="spin" size={24} /> : <Upload size={24} />}
        <strong>{fileName || "Upload CSV or Excel"}</strong>
        <span>{loading ? "Profiling dataset..." : "Drop a file here or choose one from your machine"}</span>
      </label>
      {error ? <p className="error-text">{error}</p> : null}
    </section>
  );
}
