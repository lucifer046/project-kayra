# ┌────────────────────────────────────────────────────────────────────────┐
# │                            deep_research.py                            │
# │                Autonomous Deep Synthesis Research Engine               │
# └────────────────────────────────────────────────────────────────────────┘
"""
This module implements an advanced, multi-stage autonomous deep research agent
inspired by Gemini Deep Research and ChatGPT Deep Research architectures.

Research Pipeline (6 Stages):
  1. Research Plan Generation: LLM decomposes topic into a structured research plan
     with distinct angles, sub-topics, and specific search queries.
  2. Broad Web Scraping: Executes initial search queries via DuckDuckGo to gather
     high-level context snippets from the web.
  3. Deep Page Extraction: For the most relevant search results, fetches full webpage
     content and extracts clean article text using BeautifulSoup.
  4. Follow-Up Query Generation: LLM analyzes gathered data to identify knowledge gaps
     and generates targeted follow-up queries to fill them.
  5. Gap-Filling Scrape: Executes follow-up queries and deep-scrapes additional pages.
  6. Final Synthesis: LLM synthesizes ALL gathered context into an exhaustive,
     publication-grade Markdown research document with citations.
"""

import os
import re
import time
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from dotenv import dotenv_values

# Fallback import handles modern ddgs vs legacy duckduckgo_search libraries
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

# Robust relative path imports across standalone and package execution
try:
    from .llm_engine import CentralizedLLMEngine
except ImportError:
    try:
        from modules.llm_engine import CentralizedLLMEngine
    except ImportError:
        from llm_engine import CentralizedLLMEngine

try:
    from .utils import print_banner, print_info, print_success, print_warning, print_error, print_system, console
except ImportError:
    try:
        from modules.utils import print_banner, print_info, print_success, print_warning, print_error, print_system, console
    except ImportError:
        from utils import print_banner, print_info, print_success, print_warning, print_error, print_system, console

# ┌────────────────────────────────────────────────────────────────────────┐
# │                            CONFIGURATION                               │
# └────────────────────────────────────────────────────────────────────────┘

# Resolve absolute pathways to dynamically locate .env from any execution directory
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) if __file__ else "."
env_vars = dotenv_values(os.path.join(root, ".env")) or {}

# Research depth parameters (configurable via .env)
MAX_SUB_QUESTIONS = int(env_vars.get("MAX_SUB_QUESTIONS", "5"))
MAX_FOLLOWUP_QUERIES = int(env_vars.get("MAX_FOLLOWUP_QUERIES", "3"))
MAX_DEEP_PAGES = int(env_vars.get("MAX_DEEP_PAGES", "4"))
SEARCH_RESULTS_PER_QUERY = int(env_vars.get("SEARCH_RESULTS_PER_QUERY", "6"))

# HTTP session for connection pooling across multiple page fetches
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
})

# Initialize centralized intelligence switcher matrix
engine = CentralizedLLMEngine()

# Ensure dedicated Reports output directory exists
REPORTS_DIR = os.path.join(root, "Reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# ┌────────────────────────────────────────────────────────────────────────┐
# │                    STAGE 1: RESEARCH PLAN GENERATION                    │
# └────────────────────────────────────────────────────────────────────────┘

def generate_research_plan(topic):
    """
    Uses the LLM to create a structured research plan that decomposes the topic
    into distinct research angles, each with specific search queries.

    Parameters:
        topic (str): The broad central research topic.

    Returns:
        dict: A structured plan with 'angles' (list of angle dicts with name and queries).
    """
    print_info(f"Generating structured research plan for: '{topic}'...")

    prompt = f"""You are an expert research strategist. Create a detailed research plan for the following topic.

Topic: {topic}

Break this into {MAX_SUB_QUESTIONS} distinct research angles/perspectives. For each angle, provide:
1. A short descriptive name for the angle (2-5 words)
2. Two highly specific search queries optimized for finding authoritative information

Output format (follow EXACTLY):
ANGLE: [angle name]
QUERY: [first search query]
QUERY: [second search query]

ANGLE: [angle name]
QUERY: [first search query]
QUERY: [second search query]

... and so on for all {MAX_SUB_QUESTIONS} angles. No extra text, no numbering, no bullet points."""

    api_messages = [{"role": "user", "content": prompt}]

    response_text = ""
    for chunk in engine.generate_chat_stream(api_messages):
        response_text += chunk

    # Parse the structured response into a plan dict
    plan = {"angles": []}
    current_angle = None

    for line in response_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.upper().startswith("ANGLE:"):
            current_angle = {"name": line.split(":", 1)[1].strip(), "queries": []}
            plan["angles"].append(current_angle)
        elif line.upper().startswith("QUERY:") and current_angle is not None:
            query = line.split(":", 1)[1].strip()
            if query:
                current_angle["queries"].append(query)

    # Fallback: if parsing failed, use simple comma-split decomposition
    if not plan["angles"]:
        print_warning("Structured plan parsing failed. Falling back to simple decomposition...")
        simple_queries = [q.strip() for q in response_text.split(",") if q.strip()]
        plan["angles"] = [{"name": f"Aspect {i+1}", "queries": [q]} for i, q in enumerate(simple_queries[:MAX_SUB_QUESTIONS])]

    return plan

# ┌────────────────────────────────────────────────────────────────────────┐
# │                    STAGE 2: BROAD WEB SCRAPING                          │
# └────────────────────────────────────────────────────────────────────────┘

def search_web(query, max_results=None):
    """
    Executes a web search via DuckDuckGo and returns structured result objects.

    Parameters:
        query (str): The search query string.
        max_results (int): Maximum number of results to return.

    Returns:
        list: List of result dicts with 'title', 'body', and 'href' keys.
    """
    if max_results is None:
        max_results = SEARCH_RESULTS_PER_QUERY
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return results
    except Exception as e:
        print_warning(f"Search failed for '{query}': {e}")
        return []

def compile_search_context(results, query_label=""):
    """
    Formats raw DuckDuckGo search results into a structured context block.

    Parameters:
        results (list): List of search result dicts.
        query_label (str): Label describing the search query.

    Returns:
        tuple: (formatted_context_string, list_of_urls_for_deep_scraping)
    """
    if not results:
        return "", []

    context = f"\n--- Search: {query_label} ---\n"
    urls = []

    for result in results:
        title = result.get("title", "Unknown")
        body = result.get("body", "")
        href = result.get("href", "")
        context += f"[{title}]: {body}\n"
        if href:
            urls.append({"url": href, "title": title})

    return context, urls

# ┌────────────────────────────────────────────────────────────────────────┐
# │                    STAGE 3: DEEP PAGE EXTRACTION                        │
# └────────────────────────────────────────────────────────────────────────┘

def extract_page_content(url, timeout=8):
    """
    Fetches a full webpage and extracts clean article text using BeautifulSoup.
    Strips navigation, ads, scripts, and other non-content elements.

    Parameters:
        url (str): The URL to fetch and extract text from.
        timeout (int): HTTP request timeout in seconds.

    Returns:
        str: Extracted clean text content from the page, truncated to ~4000 chars.
    """
    try:
        response = SESSION.get(url, timeout=timeout, allow_redirects=True)
        if response.status_code != 200:
            return ""

        # Detect encoding from content-type header or response encoding
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return ""

        soup = BeautifulSoup(response.text, "html.parser")

        # Strip non-content tags that pollute article extraction
        for tag in soup(["script", "style", "nav", "header", "footer", "aside",
                         "form", "button", "iframe", "noscript", "svg", "img",
                         "figure", "figcaption", "menu", "input", "select", "textarea"]):
            tag.decompose()

        # Try to find the main content area first (common article containers)
        main_content = (
            soup.find("article") or
            soup.find("main") or
            soup.find("div", class_=re.compile(r"(content|article|post|entry|text|body)", re.I)) or
            soup.find("div", id=re.compile(r"(content|article|post|entry|text|body)", re.I)) or
            soup.body
        )

        if not main_content:
            return ""

        # Extract and clean text
        text = main_content.get_text(separator="\n", strip=True)

        # Remove excessive whitespace and blank lines
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        clean_text = "\n".join(lines)

        # Truncate to prevent context window overflow (approx 4000 chars per page)
        if len(clean_text) > 4000:
            clean_text = clean_text[:4000] + "\n[...content truncated...]"

        return clean_text

    except Exception:
        return ""

def deep_scrape_top_pages(url_list, max_pages=None):
    """
    Fetches full page content from the most relevant URLs discovered during search.

    Parameters:
        url_list (list): List of URL dicts with 'url' and 'title' keys.
        max_pages (int): Maximum number of pages to deep-scrape.

    Returns:
        str: Combined deep-scraped context from all extracted pages.
    """
    if max_pages is None:
        max_pages = MAX_DEEP_PAGES

    deep_context = ""
    scraped_count = 0

    # Prioritize non-social-media, non-video URLs for content quality
    skip_domains = ["youtube.com", "twitter.com", "x.com", "facebook.com",
                    "instagram.com", "tiktok.com", "reddit.com", "pinterest.com"]

    for item in url_list:
        if scraped_count >= max_pages:
            break

        url = item.get("url", "")
        title = item.get("title", "")

        if any(domain in url.lower() for domain in skip_domains):
            continue

        print_info(f"  Deep extracting: {title[:50]}...")
        content = extract_page_content(url)

        if content and len(content) > 200:
            deep_context += f"\n=== Deep Source: {title} ===\n"
            deep_context += f"URL: {url}\n"
            deep_context += content + "\n"
            scraped_count += 1

    return deep_context

# ┌────────────────────────────────────────────────────────────────────────┐
# │                  STAGE 4: FOLLOW-UP QUERY GENERATION                    │
# └────────────────────────────────────────────────────────────────────────┘

def generate_followup_queries(topic, existing_context):
    """
    Analyzes the gathered context and identifies knowledge gaps.
    Generates targeted follow-up queries to fill those gaps.

    Parameters:
        topic (str): The original research topic.
        existing_context (str): The context gathered so far (truncated for prompt).

    Returns:
        list: A list of follow-up search query strings.
    """
    print_info("Analyzing knowledge gaps and generating follow-up queries...")

    # Truncate context to fit within reasonable prompt limits
    truncated_context = existing_context[:6000] if len(existing_context) > 6000 else existing_context

    prompt = f"""You are a research analyst reviewing gathered data for completeness.

Original Research Topic: {topic}

Here is a summary of what has been gathered so far:
{truncated_context}

Identify {MAX_FOLLOWUP_QUERIES} critical knowledge gaps or missing perspectives that would significantly improve the research quality.
For each gap, write one highly specific search query to fill it.

Output ONLY a comma-separated list of {MAX_FOLLOWUP_QUERIES} search queries. No extra text, no numbering, no bullet points."""

    api_messages = [{"role": "user", "content": prompt}]

    response_text = ""
    for chunk in engine.generate_chat_stream(api_messages):
        response_text += chunk

    queries = [q.strip() for q in response_text.split(",") if q.strip()]
    return queries[:MAX_FOLLOWUP_QUERIES]

# ┌────────────────────────────────────────────────────────────────────────┐
# │                       STAGE 5: GAP-FILLING SCRAPE                       │
# └────────────────────────────────────────────────────────────────────────┘

def execute_followup_research(followup_queries):
    """
    Executes follow-up search queries and performs deep page extraction on results.

    Parameters:
        followup_queries (list): List of follow-up search query strings.

    Returns:
        str: Combined context from follow-up research.
    """
    followup_context = ""

    for i, query in enumerate(followup_queries, 1):
        print_info(f"Follow-up scrape [{i}/{len(followup_queries)}]: {query[:60]}...")
        results = search_web(query, max_results=4)

        snippet_ctx, urls = compile_search_context(results, query)
        followup_context += snippet_ctx

        # Deep-scrape top 2 pages from follow-up results
        if urls:
            deep_ctx = deep_scrape_top_pages(urls, max_pages=2)
            followup_context += deep_ctx

    return followup_context

# ┌────────────────────────────────────────────────────────────────────────┐
# │                    STAGE 6: FINAL SYNTHESIS ENGINE                      │
# └────────────────────────────────────────────────────────────────────────┘

def synthesize_report(topic, full_context):
    """
    Instructs the LLM to synthesize ALL gathered raw context into an exhaustive,
    publication-grade Markdown research document.

    Parameters:
        topic (str): The original research topic.
        full_context (str): All gathered context data (search snippets + deep page extractions).

    Returns:
        str: The full synthesized Markdown report.
    """
    # Truncate context if it exceeds safe limits (keeping most recent/relevant data)
    max_ctx = 30000
    if len(full_context) > max_ctx:
        full_context = full_context[:max_ctx] + "\n\n[...additional context truncated for synthesis...]"

    synthesis_prompt = f"""You are a world-class research analyst and technical writer. Write an exhaustive, deeply detailed, publication-grade research report on the following topic.

You MUST base your analysis ONLY on the provided context data. Do NOT fabricate facts.

Topic: {topic}
Date: {datetime.now().strftime('%B %d, %Y')}

=== REQUIREMENTS ===
1. STRUCTURE: Use professional Markdown formatting with clear hierarchy:
   - Title (H1)
   - Executive Summary (concise overview of key findings)
   - Table of Contents
   - Multiple Deep Dive Sections (H2/H3) organized by theme
   - Key Findings & Analysis
   - Challenges & Limitations
   - Future Outlook / Implications
   - Conclusion
   - Sources Referenced

2. DEPTH: Each section should have substantial content (multiple paragraphs).
   Go deep into technical details, mechanisms, comparisons, and implications.
   Use data points, statistics, and specific examples from the context.

3. QUALITY:
   - Write in a professional, objective academic tone.
   - Use bold text for key terms and italics for emphasis.
   - Include bullet points and numbered lists where appropriate.
   - If the context contains contradictions, note and analyze them.
   - Draw connections between different pieces of information.
   - Provide critical analysis, not just information aggregation.

4. CITATIONS: Where possible, reference the source titles provided in the context blocks.

5. LENGTH: This should be a comprehensive document (aim for 2000+ words).

Do NOT include any conversational text like "Here is your report" or "Based on the provided context".
Start directly with the title.

=== RAW RESEARCH CONTEXT ===
{full_context}"""

    api_messages = [{"role": "user", "content": synthesis_prompt}]

    final_report = ""
    console.print("\n[bold white]Generating Document Live:[/bold white]\n", style="dim")

    for chunk in engine.generate_chat_stream(api_messages):
        console.print(chunk, end="", style="cyan")
        final_report += chunk

    console.print("\n")
    return final_report

# ┌────────────────────────────────────────────────────────────────────────┐
# │                          MAIN ORCHESTRATOR                              │
# └────────────────────────────────────────────────────────────────────────┘

def DeepResearchEngine(topic):
    """
    Orchestrates the full 6-stage advanced deep research pipeline:

    Stage 1 -- Research Plan: LLM generates structured research angles and queries.
    Stage 2 -- Broad Scrape: Executes all planned queries via DuckDuckGo.
    Stage 3 -- Deep Extract: Fetches and parses full page content from top results.
    Stage 4 -- Gap Analysis: LLM identifies missing knowledge and generates follow-ups.
    Stage 5 -- Gap Fill: Executes follow-up queries with additional deep page scraping.
    Stage 6 -- Synthesis: LLM writes an exhaustive publication-grade Markdown report.
    """
    try:
        console.print("\n[bold magenta]╔══════════════════════════════════════════════════════╗[/bold magenta]")
        console.print("[bold magenta]║     ADVANCED DEEP RESEARCH PROTOCOL INITIATED       ║[/bold magenta]")
        console.print("[bold magenta]╚══════════════════════════════════════════════════════╝[/bold magenta]\n")

        start_time = time.time()

        # ═══════════════════════════════════════════════════
        # STAGE 1: Generate Research Plan
        # ═══════════════════════════════════════════════════
        console.print("[bold yellow]▸ STAGE 1/6:[/bold yellow] Generating Research Plan...\n")
        plan = generate_research_plan(topic)

        total_queries = sum(len(a["queries"]) for a in plan["angles"])
        print_success(f"Research plan created: {len(plan['angles'])} angles, {total_queries} queries")

        for angle in plan["angles"]:
            console.print(f"  [dim]▹ {angle['name']}[/dim]")
            for q in angle["queries"]:
                console.print(f"    [dim]  → {q[:70]}[/dim]")

        # ═══════════════════════════════════════════════════
        # STAGE 2: Broad Web Scraping
        # ═══════════════════════════════════════════════════
        console.print(f"\n[bold yellow]▸ STAGE 2/6:[/bold yellow] Executing Broad Web Scraping...\n")
        master_context = ""
        all_urls = []
        query_count = 0

        for angle in plan["angles"]:
            for query in angle["queries"]:
                query_count += 1
                print_info(f"Searching [{query_count}/{total_queries}]: {query[:60]}...")
                results = search_web(query)
                snippet_ctx, urls = compile_search_context(results, query)
                master_context += snippet_ctx
                all_urls.extend(urls)

        print_success(f"Broad scrape complete: {len(all_urls)} source URLs discovered")

        # ═══════════════════════════════════════════════════
        # STAGE 3: Deep Page Extraction
        # ═══════════════════════════════════════════════════
        console.print(f"\n[bold yellow]▸ STAGE 3/6:[/bold yellow] Deep Page Content Extraction...\n")

        # Deduplicate URLs by domain to get diverse sources
        seen_domains = set()
        unique_urls = []
        for item in all_urls:
            try:
                domain = item["url"].split("//")[-1].split("/")[0]
                if domain not in seen_domains:
                    seen_domains.add(domain)
                    unique_urls.append(item)
            except (IndexError, KeyError):
                continue

        deep_context = deep_scrape_top_pages(unique_urls)
        master_context += deep_context
        print_success(f"Deep extraction complete. Total context: {len(master_context):,} characters")

        if not master_context.strip():
            print_error("Failed to retrieve sufficient data blocks. Aborting research.")
            return

        # ═══════════════════════════════════════════════════
        # STAGE 4: Follow-Up Query Generation (Gap Analysis)
        # ═══════════════════════════════════════════════════
        console.print(f"\n[bold yellow]▸ STAGE 4/6:[/bold yellow] Analyzing Knowledge Gaps...\n")
        followup_queries = generate_followup_queries(topic, master_context)

        if followup_queries:
            print_success(f"Identified {len(followup_queries)} knowledge gaps to fill:")
            for fq in followup_queries:
                console.print(f"  [dim]  → {fq[:70]}[/dim]")
        else:
            print_info("No significant knowledge gaps detected. Proceeding to synthesis.")

        # ═══════════════════════════════════════════════════
        # STAGE 5: Gap-Filling Research
        # ═══════════════════════════════════════════════════
        if followup_queries:
            console.print(f"\n[bold yellow]▸ STAGE 5/6:[/bold yellow] Filling Knowledge Gaps...\n")
            followup_context = execute_followup_research(followup_queries)
            master_context += followup_context
            print_success(f"Gap-filling complete. Final context pool: {len(master_context):,} characters")
        else:
            console.print(f"\n[bold yellow]▸ STAGE 5/6:[/bold yellow] [dim]Skipped (no gaps detected)[/dim]\n")

        # ═══════════════════════════════════════════════════
        # STAGE 6: Final Synthesis
        # ═══════════════════════════════════════════════════
        console.print(f"[bold yellow]▸ STAGE 6/6:[/bold yellow] Synthesizing Research Document...\n")
        print_system(f"Total context pool size: {len(master_context):,} characters from {len(seen_domains)} unique domains")

        final_report = synthesize_report(topic, master_context)

        # ═══════════════════════════════════════════════════
        # EXPORT: Save to Markdown file
        # ═══════════════════════════════════════════════════
        clean_topic = re.sub(r'[^a-zA-Z0-9_\- ]', '', topic)
        clean_topic = clean_topic.replace(" ", "_")[:40]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{clean_topic}_{timestamp}.md"
        filepath = os.path.join(REPORTS_DIR, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(final_report)

        elapsed = time.time() - start_time
        elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s" if elapsed >= 60 else f"{elapsed:.1f}s"

        console.print(f"\n[bold green]╔══════════════════════════════════════════════════════╗[/bold green]")
        console.print(f"[bold green]║          RESEARCH PROTOCOL COMPLETE                  ║[/bold green]")
        console.print(f"[bold green]╠══════════════════════════════════════════════════════╣[/bold green]")
        console.print(f"[bold green]║[/bold green]  Document: [cyan]{filename}[/cyan]")
        console.print(f"[bold green]║[/bold green]  Location: [cyan]{filepath}[/cyan]")
        console.print(f"[bold green]║[/bold green]  Duration: [cyan]{elapsed_str}[/cyan]")
        console.print(f"[bold green]║[/bold green]  Sources:  [cyan]{len(seen_domains)} unique domains[/cyan]")
        console.print(f"[bold green]║[/bold green]  Context:  [cyan]{len(master_context):,} characters processed[/cyan]")
        console.print(f"[bold green]╚══════════════════════════════════════════════════════╝[/bold green]")

    except Exception as e:
        print_error(f"Deep Research fatal failure: {e}")

# ┌────────────────────────────────────────────────────────────────────────┐
# │                         DIAGNOSTIC TEST NODE                           │
# └────────────────────────────────────────────────────────────────────────┘

if __name__ == "__main__":
    # Test entrypoint to execute research tasks locally in an interactive terminal sandbox
    print_banner("KAYRA RESEARCH ENGINE", "Advanced Deep Synthesis Sandbox")

    while True:
        try:
            user_topic = console.input("\n[bold cyan]Target Topic > [/bold cyan] ").strip()

            if not user_topic:
                continue

            if user_topic.lower() in ["exit", "quit", "bye"]:
                print_system("Terminating research workspace.")
                break

            DeepResearchEngine(user_topic)

        except KeyboardInterrupt:
            console.print()
            print_system("Manual interrupt. Shutting down research agent.")
            break