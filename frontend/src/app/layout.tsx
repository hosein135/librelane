import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LibreLane Web",
  description: "LibreLane Colab notebook as a web application",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
