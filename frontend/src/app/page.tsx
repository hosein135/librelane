import { HomeClient } from "@/components/flow/HomeClient";
import { requireUsername } from "@/lib/server-auth";

export default async function HomePage({
  searchParams,
}: {
  searchParams: Promise<{ id?: string }>;
}) {
  const username = await requireUsername();
  const sp = await searchParams;
  const id = Number(sp.id || "");
  const selectedRunId = Number.isFinite(id) && id > 0 ? id : null;
  return <HomeClient username={username} selectedRunId={selectedRunId} />;
}
