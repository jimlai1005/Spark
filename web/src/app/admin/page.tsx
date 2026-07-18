"use client";
import { useQuery } from "@tanstack/react-query";
import { ApiError, getAdminPending } from "@/lib/api";
import { COPY } from "@/lib/copy";

export default function AdminPage() {
  const c = COPY.admin;
  const q = useQuery({ queryKey: ["admin-pending"], queryFn: getAdminPending });

  if (q.isLoading) {
    return <main className="page"><p className="hint">{COPY.common.loading}</p></main>;
  }
  if (q.error) {
    const forbidden = q.error instanceof ApiError && q.error.status === 403;
    return (
      <main className="page">
        <p>{forbidden ? c.forbidden : COPY.common.notLoggedIn}</p>
      </main>
    );
  }

  const pending = q.data?.pending ?? [];
  return (
    <main className="page">
      <p className="eyebrow">ADMIN</p>
      <h1>{c.title}</h1>
      <p className="hint">{c.note}</p>
      {pending.length === 0 ? (
        <p>{c.empty}</p>
      ) : (
        <div className="panel">
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">{c.cols.account}</th>
                <th scope="col">{c.cols.user}</th>
                <th scope="col">{c.cols.agent}</th>
                <th scope="col">{c.cols.builder}</th>
                <th scope="col">{c.cols.network}</th>
                <th scope="col">{c.cols.label}</th>
              </tr>
            </thead>
            <tbody>
              {pending.map((e) => (
                <tr key={e.account_id}>
                  <td className="mono">{e.account_id}</td>
                  <td className="mono">{e.user_address}</td>
                  <td className="mono">{e.agent_address}</td>
                  <td className="mono">{e.builder_address}</td>
                  <td>{e.network}</td>
                  <td>{e.label}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
