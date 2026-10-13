"""日志配置（可观测性子系统）。

这个文件属于「可观测性子系统」，负责把程序运行过程中的关键信息（什么时候、什么模块、
说了什么）统一打印出来，方便调试和排错。

关键内容：
- 只有一个函数 configure_logging()：对 Python 标准库 logging 做一次性基础配置。

和其他模块的配合：
- main.py 在应用启动时通常会调用它，让整站日志格式统一；
- 其他模块用 `import logging; logger = logging.getLogger(__name__)` 打日志时，
  就会沿用这里的格式。可以把它看作「给整个项目配了一台统一格式的打印机」。."""

# 引入 Python 内置的 logging 模块（标准库，用来记录日志）
import logging


# 定义一个函数：配置日志，无返回值（只做设置）
def configure_logging() -> None:
    # 调用标准库的基础配置：设置日志的最低级别、以及每行日志长什么样
    logging.basicConfig(
        # 级别设为 INFO：INFO 及以上（WARNING/ERROR）都会打印，DEBUG 这类最细的默认不打印
        level=logging.INFO,
        # 格式串：时间 + 级别 + [模块名] + 消息。%(asctime)s 等是占位符，运行时会替换成真实值
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
