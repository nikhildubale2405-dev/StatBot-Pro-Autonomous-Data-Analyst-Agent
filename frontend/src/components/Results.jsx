import { BarChart3, Table2 } from "lucide-react";
import { chartUrl } from "../lib/api.js";

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
  return (
    <div className="result-block chart-block">
      <div className="result-title">
        <BarChart3 size={16} />
        <span>{chart.title || "Chart"}</span>
      </div>
      <img src={chartUrl(chart.url)} alt={chart.title || "Generated analysis chart"} />
    </div>
  );
}
