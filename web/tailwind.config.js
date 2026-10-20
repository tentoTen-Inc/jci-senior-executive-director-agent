/** @type {import('tailwindcss').Config} */
// デザインの決まりは docs/admin-ui-design.md §2（C案「LINE なじみ」）
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // 旧デザインの名前（移行中の画面のため残す）
        navy: "#111111",
        brand: "#06C755",
        line: { DEFAULT: "#06C755", dark: "#05A847", soft: "#ECFDF3", ink: "#067A39" },
        ink: { DEFAULT: "#111111", sub: "#6B7280", faint: "#9CA3AF" },
        canvas: "#F7F7F8",
        danger: { DEFAULT: "#D92D20", soft: "#FEF3F2" },
        hold: { DEFAULT: "#B54708", soft: "#FFFAEB" },
        info: { DEFAULT: "#175CD3", soft: "#EFF8FF" },
      },
      fontFamily: {
        sans: ['"M PLUS 1p"', '"Hiragino Sans"', '"Noto Sans JP"', "sans-serif"],
      },
      borderRadius: { card: "18px" },
    },
  },
  plugins: [],
};
