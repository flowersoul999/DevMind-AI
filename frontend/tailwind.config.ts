// 引入 Tailwind 的类型定义，方便编辑器提示，不影响运行。
import type { Config } from "tailwindcss";

// Tailwind 的配置文件。把 Tailwind 类比成「一套现成的积木样式库」，
//   这个文件就是告诉它：去哪些文件里找我们用到的类名、以及自定义哪些专属积木（颜色等）。
const config: Config = {
  // content：告诉 Tailwind 去扫描哪些文件里的 class 名。
  //   只有被扫描到的类名才会被打包进最终 CSS，没用到的类名会被自动删掉，减小打包体积。
  // 这里扫描 app、components、lib 三个目录下的 .ts/.tsx 文件（即我们写界面的地方）。
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],

  // theme：主题配置，也就是「这套积木有哪些颜色、字号、间距……」。
  theme: {
    // extend：在 Tailwind 默认主题基础上「追加 / 覆盖」，而不是整体替换。
    // 为什么用 extend 而不直接写 theme：保留 Tailwind 自带的蓝/红/灰等默认颜色，只补充本项目专属色板。
    extend: {
      // 自定义一组项目专属颜色 token（像给颜色起外号，方便全站统一调用）。
      colors: {
        ink: "#17212b",   // 主文字色：深墨蓝灰，用于正文 / 标题
        panel: "#f7f9fb", // 面板底色：极浅灰蓝，用于卡片 / 侧边栏背景
        line: "#d8e0e8",  // 描边色：浅灰蓝，用于边框 / 分隔线
        accent: "#0f766e",// 强调色：青绿，用于按钮 / 重点高亮（品牌色）
        warn: "#b45309"   // 警告色：琥珀棕，用于提示 / 警示信息
      }
    }
  },

  // plugins：Tailwind 插件数组。这里为空，表示暂不使用额外插件（如官方排版、表单等插件）。
  plugins: []
};

// 导出配置，Tailwind 构建时会读取。
export default config;
