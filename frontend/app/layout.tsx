import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Каталог",
  description: "Фотографии и названия товаров в одном каталоге.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ru">
      <body className="min-h-dvh bg-neutral-50 font-sans text-neutral-950 antialiased">
        {children}
      </body>
    </html>
  );
}
