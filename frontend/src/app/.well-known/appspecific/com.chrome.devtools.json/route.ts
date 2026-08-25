/**
 * Chrome DevTools auto-workspace asks for this file on every navigation.
 * Next.js would answer with the Linux WSL workdir path, which a Windows browser
 * rejects as "Unable to add filesystem: <illegal path>". Return 404 so DevTools
 * skips workspace binding (editing still works in the IDE).
 */
export async function GET() {
  return new Response(null, { status: 404 });
}
