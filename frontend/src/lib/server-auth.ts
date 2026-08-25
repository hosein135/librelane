import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export async function requireUsername(): Promise<string> {
  const jar = await cookies();
  const username = jar.get("username")?.value;
  if (!username) {
    redirect("/login?reason=required");
  }
  return username;
}
