from __future__ import annotations

from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib import parse, request
import json
import re


WEB_FINDINGS_DIR = Path(__file__).resolve().parent.parent / "knowledge_base" / "web_findings"
SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"
USER_AGENT = (
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
	"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class _SimpleHTMLTextExtractor(HTMLParser):
	def __init__(self):
		super().__init__()
		self._skip_depth = 0
		self._parts: list[str] = []
		self._title_parts: list[str] = []
		self._in_title = False

	def handle_starttag(self, tag, attrs):
		if tag in {"script", "style", "noscript"}:
			self._skip_depth += 1
			return

		if tag == "title":
			self._in_title = True

		if tag in {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "li", "br"}:
			self._parts.append("\n")

	def handle_endtag(self, tag):
		if tag in {"script", "style", "noscript"} and self._skip_depth:
			self._skip_depth -= 1
			return

		if tag == "title":
			self._in_title = False

		if tag in {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "li"}:
			self._parts.append("\n")

	def handle_data(self, data):
		if self._skip_depth:
			return

		text = unescape(data or "")
		if not text.strip():
			return

		self._parts.append(text)
		if self._in_title:
			self._title_parts.append(text)

	def get_title(self):
		return _normalize_whitespace(" ".join(self._title_parts))

	def get_text(self):
		return _normalize_whitespace(" ".join(self._parts), preserve_newlines=True)


def _normalize_whitespace(text: str, preserve_newlines: bool = False):
	normalized = text.replace("\r", "\n").replace("\t", " ")
	if preserve_newlines:
		normalized = re.sub(r"\n{3,}", "\n\n", normalized)
		normalized = re.sub(r"[ \f\v]+", " ", normalized)
		normalized = re.sub(r" ?\n ?", "\n", normalized)
		return normalized.strip()
	return re.sub(r"\s+", " ", normalized).strip()


def _slugify(text: str):
	lowered = text.strip().lower()
	lowered = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", lowered)
	lowered = re.sub(r"-{2,}", "-", lowered)
	return lowered.strip("-") or "web-summary"


def _fetch_text(url: str, method: str = "GET", data: bytes | None = None):
	req = request.Request(
		url,
		data=data,
		method=method,
		headers={
			"User-Agent": USER_AGENT,
			"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
		},
	)
	with request.urlopen(req, timeout=20) as response:
		encoding = response.headers.get_content_charset() or "utf-8"
		return response.read().decode(encoding, errors="ignore")


def _extract_result_links(search_html: str, max_results: int):
	matches = re.findall(
		r'<a[^>]+class="[^\"]*result__a[^\"]*"[^>]+href="([^\"]+)"[^>]*>(.*?)</a>',
		search_html,
		flags=re.IGNORECASE | re.DOTALL,
	)

	results = []
	seen_urls = set()
	for href, title_html in matches:
		title = _normalize_whitespace(re.sub(r"<[^>]+>", " ", unescape(title_html)))
		url = unescape(href)
		if not url or url in seen_urls:
			continue
		seen_urls.add(url)
		results.append({"title": title or url, "url": url})
		if len(results) >= max_results:
			break
	return results


def _search_web(query: str, max_results: int):
	payload = parse.urlencode({"q": query}).encode("utf-8")
	search_html = _fetch_text(SEARCH_ENDPOINT, method="POST", data=payload)
	results = _extract_result_links(search_html, max_results=max_results)
	if not results:
		raise RuntimeError("No search results found from the web search endpoint.")
	return results


def _extract_page_content(url: str, max_chars: int = 6000):
	html = _fetch_text(url)
	parser = _SimpleHTMLTextExtractor()
	parser.feed(html)
	title = parser.get_title()
	text = parser.get_text()
	text = re.sub(r"\n{2,}", "\n\n", text).strip()
	return {
		"title": title,
		"content": text[:max_chars],
	}


def _fallback_summary(topic: str, collected_pages: list[dict]):
	lines = [f"主题：{topic}", "", "未能调用模型完成总结，以下为抓取到的网页摘要：", ""]
	for index, page in enumerate(collected_pages, start=1):
		excerpt = page["content"][:280].strip()
		lines.append(f"{index}. {page['title']}")
		lines.append(f"来源：{page['url']}")
		lines.append(excerpt or "(无可用正文)")
		lines.append("")
	return "\n".join(lines).strip()


def _build_summary_prompt(topic: str, collected_pages: list[dict], language: str):
	payload = {
		"topic": topic,
		"language": language,
		"sources": [
			{
				"title": page["title"],
				"url": page["url"],
				"content": page["content"],
			}
			for page in collected_pages
		],
	}

	return (
		"你是一个网页信息整理助手。请基于给定网页内容，输出一份中文 Markdown 汇总。\n"
		"要求：\n"
		"1. 开头给出 4 到 8 条要点总结。\n"
		"2. 然后给出一个分章节的详细整理。\n"
		"3. 如果不同来源之间存在冲突，要明确标注。\n"
		"4. 不要编造来源中没有的信息。\n"
		"5. 最后追加一个“来源说明”小节，按标题列出来源及用途。\n\n"
		f"数据：\n{json.dumps(payload, ensure_ascii=False)}"
	)


def _resolve_output_path(topic: str, output_path: str = ""):
	if output_path.strip():
		target = Path(output_path).expanduser()
	else:
		WEB_FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
		filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_slugify(topic)}.md"
		target = WEB_FINDINGS_DIR / filename

	target.parent.mkdir(parents=True, exist_ok=True)
	return target


def register(agent):
	def search_web_and_save(topic: str, max_results: int = 5, output_path: str = "", language: str = "zh-CN"):
		topic_text = (topic or "").strip()
		if not topic_text:
			raise ValueError("topic is required")

		result_limit = max(1, min(int(max_results), 8))
		search_results = _search_web(topic_text, max_results=result_limit)

		collected_pages = []
		errors = []
		for result in search_results:
			try:
				page = _extract_page_content(result["url"])
			except Exception as exc:
				errors.append(f"- {result['url']}: {exc}")
				continue

			content = page.get("content", "").strip()
			if not content:
				errors.append(f"- {result['url']}: empty content")
				continue

			collected_pages.append(
				{
					"title": page.get("title") or result["title"],
					"url": result["url"],
					"content": content,
				}
			)

		if not collected_pages:
			details = "\n".join(errors) if errors else "No page content could be extracted."
			raise RuntimeError(f"Search completed, but no page content could be extracted.\n{details}")

		summary_markdown = ""
		try:
			get_active_model = getattr(agent, "_get_active_model_name", None)
			model_name = get_active_model() if callable(get_active_model) else agent.model_name
			response = agent.chat_completion(
				model=model_name,
				messages=[
					{
						"role": "user",
						"content": _build_summary_prompt(topic_text, collected_pages, language),
					}
				],
			)
			summary_markdown = (response.get("message") or {}).get("content", "").strip()
		except Exception:
			summary_markdown = ""

		if not summary_markdown:
			summary_markdown = _fallback_summary(topic_text, collected_pages)

		target = _resolve_output_path(topic_text, output_path=output_path)
		source_lines = [f"- [{page['title']}]({page['url']})" for page in collected_pages]

		report_parts = [
			f"# 网页主题汇总：{topic_text}",
			"",
			f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
			f"- 搜索结果数：{len(search_results)}",
			f"- 成功抓取数：{len(collected_pages)}",
			"",
			"## 汇总",
			"",
			summary_markdown.strip(),
			"",
			"## 来源链接",
			"",
			"\n".join(source_lines),
		]

		if errors:
			report_parts.extend([
				"",
				"## 抓取失败记录",
				"",
				"\n".join(errors),
			])

		target.write_text("\n".join(report_parts).strip() + "\n", encoding="utf-8")
		return (
			f"Saved web summary for '{topic_text}' to {target}. "
			f"Sources used: {len(collected_pages)}."
		)

	agent.add_skill(
		name="search_web_and_save",
		func=search_web_and_save,
		description=(
			"Search the public web for a topic, extract useful page content, summarize it, "
			"and save the result as a local markdown file."
		),
		parameters={
			"topic": "string",
			"max_results": "integer",
			"output_path": "string",
			"language": "string",
		},
	)
