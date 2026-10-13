// @type 是 JSDoc 类型注解：告诉编辑器/TS 这个对象符合 Next.js 的 NextConfig 类型，
// 写代码时能获得智能提示，不影响实际运行逻辑。
/** @type {import('next').NextConfig} */

// 定义 Next.js 的整体配置对象。Next 在启动开发服务器或打包构建时都会读取这个变量。
const nextConfig = {
  // distDir：指定构建产物（编译后生成的文件）存放的目录。
  // 为什么：开发时用 ".next-dev"，上线打包时用 ".next"，
  //   把两套产物分开，避免开发时残留的文件污染生产构建，也方便单独清理缓存。
  // 这里根据环境变量 NODE_ENV 判断当前是 "development"（开发）还是其它（视为生产）。
  distDir: process.env.NODE_ENV === "development" ? ".next-dev" : ".next",

  // env：把指定的环境变量“注入”到前端代码里，让浏览器端也能读到。
  env: {
    // 以 NEXT_PUBLIC_ 开头的变量会被打包进前端，组件里可用 process.env.NEXT_PUBLIC_API_BASE_URL 读取。
    // 这里优先取系统里的同名环境变量，取不到时回退（|| "..."）到默认地址 http://localhost:8000，
    //   也就是本地后端 FastAPI 服务默认监听的地址。
    // 为什么：前端要调用后端接口，接口地址不能写死在每一处代码里，统一在这里配置最省事、最好改。
    NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000"
  }
};

// 把配置导出，Next.js 启动时加载本文件就能拿到 nextConfig。
module.exports = nextConfig;
