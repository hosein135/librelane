import { redirect } from "next/navigation";

/** Legacy `/run?id=` URL → run steps page. */
export default async function LegacyRunPage({
  searchParams,
}: {
  searchParams: Promise<{ id?: string; watch?: string }>;
}) {
  const sp = await searchParams;
  const id = Number(sp.id || "");
  if (!Number.isFinite(id) || id <= 0) {
    redirect("/");
  }
  const q = sp.watch === "1" ? "?watch=1" : "";
  redirect(`/runs/${id}${q}`);
}
