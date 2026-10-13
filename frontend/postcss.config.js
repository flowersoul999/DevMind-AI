// PostCSS 是「CSS 预处理管道」：构建时会按顺序跑一系列插件来加工我们的 CSS。
// 这里只配插件，Tailwind 生成的样式和浏览器前缀都靠它自动完成。
module.exports = {
  // plugins：要启用的 PostCSS 插件列表（数组里的顺序即执行顺序）
  plugins: {
    // tailwindcss：运行 Tailwind，把我们在 JSX 里用到的类名「翻译」成真正的 CSS 规则。
    tailwindcss: {},
    // autoprefixer：自动给 CSS 加浏览器厂商前缀（如 -webkit-、-moz-）。
    // 为什么：不同浏览器需要不同前缀才能生效，手写又烦又易漏，交给它自动补全最稳。
    autoprefixer: {}
  }
};
