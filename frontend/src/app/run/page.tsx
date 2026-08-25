import { redirect } from "next/navigation";

/** Legacy `/run?id=` overview URL → home overview layer. */
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
  if (sp.watch === "1") {
    redirect(`/runs/${id}?watch=1`);
  }
  redirect(`/?id=${id}`);
}
