// ============================================================================
// lib/api.ts —— 前端「统一外卖电话」封装层
// ----------------------------------------------------------------------------
// 这个文件把项目中所有「请求后端 FastAPI 接口」的操作集中封装成一个个小函数。
// 为什么这样做：想象后端有几十个接口，如果每页都自己写 fetch，一旦后端地址或
//   报错格式变了，就得改几十处。这里统一收口，所有页面都只调用 apiGet/apiPost 等，
//   就像公司只留一个「外卖总机」，大家都打这个号，接线员（本文件）负责转接和异常处理。
// 导出的函数：
//   API_BASE_URL  后端基础地址（来自环境变量，没配就用本地 8000）
//   apiGet        发 GET 请求（查数据）
//   apiPost       发 POST 请求（新建/提交）
//   apiPatch      发 PATCH 请求（局部更新）
//   apiDelete     发 DELETE 请求（删除）
//   apiUpload     发带文件的 POST（FormData，用于上传文档）
//   apiPostStream 发 POST 但走「流式响应」（用于聊天逐字输出，SSE 协议）
//   throwApiError 内部工具：把后端报错整理成可读的 Error
// ============================================================================

// 后端基础地址：优先读环境变量 NEXT_PUBLIC_API_BASE_URL，读不到就用本地 8000。
// 为什么用 ?? 而不是 ||：?? 只在「null/undefined」时才回退，不会把空字符串误判成要替换的值。
export const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

// apiGet：用 GET 方法请求后端，并把返回的 JSON 解析成类型 T 返回。
// <T> 是泛型，表示「调用方知道返回数据长什么样」，用来给 TypeScript 做类型提示。
export async function apiGet<T>(path: string): Promise<T> {
  // fetch 是浏览器内置的「发网络请求」函数；这里拼出完整地址 基础地址+路径。
  // cache: "no-store" 表示不要缓存这次结果，保证每次都拿到最新数据（适合看实时状态）。
  const response = await fetch(`${API_BASE_URL}${path}`, { cache: "no-store" });
  // response.ok 为 false 表示 HTTP 状态码不是 2xx（比如 404/500），此时抛出整理好的错误。
  if (!response.ok) await throwApiError(response);
  // 把响应体按 JSON 解析并返回（Promise<T> 由 async 函数隐式包了一层）。
  return response.json();
}

// apiPost：用 POST 方法提交数据，常用于「创建新资源」或「执行某个动作」。
// body 用 unknown 表示「任意可序列化的数据」，由调用方决定传什么。
export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST", // 明确指定 HTTP 方法为 POST
    headers: { "Content-Type": "application/json" }, // 告诉后端：我发的是 JSON 文本
    body: JSON.stringify(body) // 把 JS 对象转成 JSON 字符串再发送
  });
  if (!response.ok) await throwApiError(response);
  return response.json();
}

// apiPatch：用 PATCH 方法做「局部更新」。和 POST 类似，但语义上只改对象的某些字段。
export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "PATCH", // 局部更新（区别于 PUT 的全量替换）
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) await throwApiError(response);
  return response.json();
}

// apiUpload：上传文件专用。文件不能用 JSON 传，要用 FormData（浏览器表单格式）。
// 注意这里没有手动设 Content-Type，因为浏览器给 FormData 发送时会自动带上正确的
//   multipart/form-data 边界标识；手动设反而容易写错。
export async function apiUpload<T>(path: string, body: FormData): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    body // 直接传 FormData，包含文件和其他字段
  });
  if (!response.ok) await throwApiError(response);
  return response.json();
}

// apiPostStream：流式 POST。用于聊天场景——后端像打字机一样「一段一段」把回答传回来，
//   而不是等全部想完再一次性返回。前端每收到一小段就回调 onEvent 让页面更新。
// 参数说明：
//   path      接口路径
//   body      提交的数据
//   onEvent   收到一个事件时的回调，event 是事件名、data 是事件携带的数据
//   options   可选配置，目前只有 signal（用于「取消请求」，比如用户点了停止生成）
export async function apiPostStream(
  path: string,
  body: unknown,
  onEvent: (event: string, data: unknown) => void,
  options: { signal?: AbortSignal } = {}
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: options.signal // 把「取消信号」交给 fetch，用户取消时请求会被中断
  });
  if (!response.ok) await throwApiError(response);
  // 流式响应必须拿到可读的原始字节流（response.body），拿不到就直接报错。
  if (!response.body) throw new Error("Streaming response body is not available.");

  // reader 用来一点点「读」后端推来的数据；decoder 把字节解码成字符串。
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = ""; // 缓冲区：网络可能把一条消息拆成两半传来，先攒着，凑齐再解析

  // 无限循环，直到后端发完（done 为 true）才 break 退出。
  while (true) {
    // 读下一段数据；done 为 true 表示流结束，value 是本次读到的字节。
    const { done, value } = await reader.read();
    if (done) break; // 流结束，跳出循环
    // 把本次字节解码成文本并追加到缓冲区；{ stream: true } 告诉解码器「后面还有数据，别急着收尾」。
    buffer += decoder.decode(value, { stream: true });
    // SSE 协议里，每条事件之间用「空行」（\n\n）分隔，所以先按双换行切开。
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? ""; // 最后一段可能不完整，先放回 buffer 等下次拼接
    // 遍历每一条完整的事件文本
    for (const rawEvent of events) {
      // 一条事件内部可能有多行，按单换行切开。
      const lines = rawEvent.split("\n");
      // 找以 "event: " 开头的那行，取出事件名；找不到就默认 "message"。
      // 为什么用 ?.：某行可能不存在，?. 避免「对 undefined 调用 slice」而报错。
      const event = lines.find((line) => line.startsWith("event: "))?.slice(7) ?? "message";
      // 把开头是 "data: " 的所有行拼接成 data 文本（可能多行）。
      const dataText = lines
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6)) // 去掉前缀 "data: "
        .join("\n");
      // 有 data 才回调；JSON.parse 把字符串还原成对象交给页面处理。
      if (dataText) onEvent(event, JSON.parse(dataText));
    }
  }
}

// apiDelete：发 DELETE 请求，用于删除某个资源。
export async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { method: "DELETE" });
  if (!response.ok) await throwApiError(response);
  return response.json();
}

// throwApiError：内部函数（不导出），负责把一次失败的响应整理成 Error 抛出。
// 返回类型写 Promise<never> 表示「这个函数永远正常返回不了，只会抛异常」。
async function throwApiError(response: Response): Promise<never> {
  // 兜底错误信息：用 HTTP 状态码+状态文本，例如 "404 Not Found"。
  const fallback = `${response.status} ${response.statusText}`.trim();
  // 看后端返回的 content-type，判断错误体是 JSON 还是纯文本。
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    let message: string | null = null;
    try {
      // 尝试把错误体按 JSON 解析，再用下面的 readApiErrorMessage 取出人话错误信息。
      const payload = await response.json();
      message = readApiErrorMessage(payload);
    } catch {
      // JSON 解析也失败就当作没有可读信息，message 保持 null。
      message = null;
    }
    // 抛出一个带清晰文案的 Error，页面 catch 后能直接展示给用户。
    throw new Error(message || fallback);
  }
  // 非 JSON 的情况（比如后端直接返回一段纯文本错误），就读文本。
  const text = (await response.text()).trim();
  throw new Error(text || fallback);
}

// readApiErrorMessage：从后端的错误响应里，尽可能挑出「人能看懂」的错误信息。
// 为什么单独抽函数：FastAPI 的报错格式不固定（有时是 {detail: "..."}，有时是
//   {detail: [{msg: "..."}]} 校验错误数组，有时是 {message: "..."}），这里统一兜底。
function readApiErrorMessage(payload: unknown): string | null {
  if (typeof payload === "string") return payload; // 最直白：整个就是字符串
  if (!payload || typeof payload !== "object") return null; // 不是对象就放弃
  // 取可能存在的 detail 字段。
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail; // 形如 { detail: "用户名已存在" }
  if (Array.isArray(detail)) {
    // 校验失败数组：形如 { detail: [{ msg: "字段必填" }, ...] }
    const messages = detail
      .map((item) => {
        // 数组里每个元素可能是 { msg: "..." } 对象，也可能是纯字符串。
        if (item && typeof item === "object" && "msg" in item) return String((item as { msg: unknown }).msg);
        return typeof item === "string" ? item : null;
      })
      .filter(Boolean); // 过滤掉没取到信息的 null
    return messages.length ? messages.join("; ") : null; // 多条信息用分号拼起来
  }
  // 再退一步：试试 { message: "..." } 这种格式。
  const message = (payload as { message?: unknown }).message;
  return typeof message === "string" ? message : null;
}
