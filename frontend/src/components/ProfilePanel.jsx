import { Columns3, Database, Sigma } from "lucide-react";

function KeyValue({ label, value }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

export default function ProfilePanel({ profile }) {
  if (!profile) {
    return (
      <section className="panel profile-panel empty-panel">
        <Database size={22} />
        <p>Upload a dataset to see its profile.</p>
      </section>
    );
  }

  const missingTotal = Object.values(profile.missing_values || {}).reduce((sum, value) => sum + Number(value || 0), 0);
  const columns = profile.columns || [];

  return (
    <section className="panel profile-panel">
      <div className="panel-title">
        <Database size={18} />
        <span>Dataset Summary</span>
      </div>
      <div className="metrics-grid">
        <KeyValue label="Rows" value={profile.shape.rows.toLocaleString()} />
        <KeyValue label="Columns" value={profile.shape.columns.toLocaleString()} />
        <KeyValue label="Missing" value={missingTotal.toLocaleString()} />
      </div>
      <div className="profile-block">
        <div className="subhead">
          <Columns3 size={16} />
          <span>Columns</span>
        </div>
        <div className="chip-list">
          {columns.map((column) => (
            <span className="chip" title={profile.data_types[column]} key={column}>
              {column}
            </span>
          ))}
        </div>
      </div>
      <div className="profile-block">
        <div className="subhead">
          <Sigma size={16} />
          <span>Sample Rows</span>
        </div>
        <div className="mini-table-wrap">
          <table className="mini-table">
            <thead>
              <tr>{columns.slice(0, 5).map((column) => <th key={column}>{column}</th>)}</tr>
            </thead>
            <tbody>
              {(profile.sample_rows || []).slice(0, 4).map((row, index) => (
                <tr key={index}>
                  {columns.slice(0, 5).map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
