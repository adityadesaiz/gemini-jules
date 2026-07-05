import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "FinTechExec Career Radar",
  description: "Career radar for fintech executives.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
