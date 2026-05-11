import { BarChart3, Table2 } from "lucide-react";
import { useEffect, useState } from "react";
import { fetchChartBlob } from "../lib/api.js";

export function TableResult({ table }) {
  return (
    <div className="result-block">
      <div className="result-title">
        <Table2 size={16} />
        <span>{table.name}</span>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>{table.columns.map((column) => <th key={column}>{column}</th>)}</tr>
          </thead>
          <tbody>
            {table.rows.map((row, index) => (
              <tr key={index}>
                {table.columns.map((column) => <td key={column}>{String(row[column] ?? "")}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ChartResult({ chart }) {
  const [src, setSrc] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let objectUrl = "";
    let cancelled = false;
    setError("");
    setSrc("");
    fetchChartBlob(chart.url)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [chart.url]);

  return (
    <div className="result-block chart-block">
      <div className="result-title">
        <BarChart3 size={16} />
        <span>{chart.title || "Chart"}</span>
      </div>
      {src ? <img src={src} alt={chart.title || "Generated analysis chart"} /> : <p className="chart-status">{error || "Loading chart..."}</p>}
    </div>
  );
}
