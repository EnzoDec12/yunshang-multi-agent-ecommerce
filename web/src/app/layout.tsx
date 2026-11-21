import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "云裳女装 · 智能穿搭导购与店铺经营台",
  description:
    "云裳女装旗舰店智能穿搭导购、订单售后服务与女装商品经营企划平台",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-[#FAF8F5] text-[#1F1D1A] antialiased">
        {children}
      </body>
    </html>
  );
}
