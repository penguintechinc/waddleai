/**
 * Code Formatting Utilities
 * Provides functions to clean up and format AI-generated code
 */

/**
 * Cleans up AI-generated code
 * Removes markdown code fences, trims whitespace, and handles common artifacts
 *
 * @param code - The raw code string to format
 * @param language - The programming language (optional, for language-specific handling)
 * @returns Formatted and cleaned code
 */
export function formatGeneratedCode(code: string, language: string = ''): string {
  if (!code || typeof code !== 'string') {
    return '';
  }

  let formatted = code;

  // Remove markdown code fences (```language ... ```)
  formatted = removeMarkdownFences(formatted);

  // Trim leading and trailing whitespace
  formatted = formatted.trim();

  // Remove common AI generation artifacts
  formatted = removeArtifacts(formatted, language);

  // Normalize indentation
  formatted = normalizeIndentation(formatted);

  // Clean up extra blank lines
  formatted = cleanupBlankLines(formatted);

  return formatted;
}

/**
 * Extracts code from markdown code blocks
 * Handles both fenced code blocks (```) and indented code blocks
 *
 * @param text - The markdown text containing code blocks
 * @returns Extracted code from the first code block found
 */
export function extractCodeFromMarkdown(text: string): string {
  if (!text || typeof text !== 'string') {
    return '';
  }

  // Try to extract from fenced code blocks first
  const fencedMatch = text.match(/```(?:\w+)?\n?([\s\S]*?)```/);
  if (fencedMatch && fencedMatch[1]) {
    return fencedMatch[1].trim();
  }

  // Try alternative fence pattern (with optional language identifier)
  const fencedAltMatch = text.match(/```[\s\S]*?\n([\s\S]*?)\n```/);
  if (fencedAltMatch && fencedAltMatch[1]) {
    return fencedAltMatch[1].trim();
  }

  // If no fenced blocks found, try to extract indented code blocks
  const lines = text.split('\n');
  let codeLines: string[] = [];
  let inCodeBlock = false;

  for (const line of lines) {
    // Indented code blocks are at least 4 spaces or 1 tab
    if (line.match(/^(\s{4,}|\t+)/)) {
      inCodeBlock = true;
      codeLines.push(line);
    } else if (inCodeBlock && line.trim() === '') {
      // Empty lines within code block
      codeLines.push('');
    } else if (inCodeBlock && !line.match(/^(\s{4,}|\t+)/)) {
      // End of code block
      break;
    }
  }

  if (codeLines.length > 0) {
    // Remove common indentation
    const minIndent = Math.min(
      ...codeLines
        .filter(line => line.trim().length > 0)
        .map(line => (line.match(/^\s*/) || [''])[0].length)
    );

    return codeLines
      .map(line => (line.length > 0 ? line.slice(minIndent) : line))
      .join('\n')
      .trim();
  }

  // If no code blocks found, return empty string
  return '';
}

/**
 * Normalizes indentation in code
 * Converts mixed indentation to consistent spacing
 *
 * @param code - The code string to normalize
 * @param spaceCount - Number of spaces to use per indentation level (default: 2)
 * @returns Code with normalized indentation
 */
export function normalizeIndentation(code: string, spaceCount: number = 2): string {
  if (!code || typeof code !== 'string') {
    return '';
  }

  const lines = code.split('\n');
  const normalized: string[] = [];

  for (const line of lines) {
    if (line.trim() === '') {
      // Keep empty lines empty
      normalized.push('');
      continue;
    }

    // Count leading whitespace (tabs and spaces)
    const leadingWhitespace = line.match(/^[\s]*/)?.[0] || '';
    const tabCount = (leadingWhitespace.match(/\t/g) || []).length;
    const spaceCount_ = (leadingWhitespace.match(/ /g) || []).length;

    // Convert tabs to spaces and calculate indentation level
    // Assume 1 tab = 2 spaces for indentation level calculation
    const totalSpaces = tabCount * spaceCount + spaceCount_;

    // Calculate the indentation level and reconstruct with consistent spaces
    const indentLevel = Math.round(totalSpaces / spaceCount);
    const newIndent = ' '.repeat(indentLevel * spaceCount);
    const trimmedLine = line.trim();

    normalized.push(newIndent + trimmedLine);
  }

  return normalized.join('\n');
}

/**
 * Removes markdown code fences from text
 *
 * @param text - The text potentially containing markdown fences
 * @returns Text without markdown fences
 */
function removeMarkdownFences(text: string): string {
  // Remove fenced code blocks with language specifiers (e.g., ```typescript)
  let result = text.replace(/^```[\w-]*\n?/gm, '');
  result = result.replace(/\n?```$/gm, '');
  result = result.replace(/```[\w-]*\s*$/gm, '');

  return result;
}

/**
 * Removes common AI generation artifacts
 *
 * @param code - The code potentially containing artifacts
 * @param language - The programming language for language-specific artifact removal
 * @returns Code without artifacts
 */
function removeArtifacts(code: string, language: string = ''): string {
  let result = code;

  // Remove common prefix artifacts
  result = result.replace(/^here\s+(?:is|are|the).*?:/im, '');
  result = result.replace(/^certainly[,\.!]?\s*/im, '');
  result = result.replace(/^of course[,\.!]?\s*/im, '');
  result = result.replace(/^sure[,\.!]?\s*/im, '');

  // Remove common suffix artifacts
  result = result.replace(/(?:this|this code|this is|that should|let me know).*$/im, '');
  result = result.replace(/hope\s+(?:this|that)\s+helps[!.]?\s*$/im, '');
  result = result.replace(/feel\s+free\s+to.*$/im, '');

  // Language-specific artifact removal
  if (language.toLowerCase() === 'python') {
    // Remove common Python artifacts
    result = result.replace(/^# This is a Python script\s*\n/i, '');
  } else if (['javascript', 'typescript', 'js', 'ts'].includes(language.toLowerCase())) {
    // Remove common JavaScript/TypeScript artifacts
    result = result.replace(/^\/\/ This is a.*?script\s*\n/i, '');
  } else if (['java', 'csharp', 'c#'].includes(language.toLowerCase())) {
    // Remove common Java/C# artifacts
    result = result.replace(/^\/\/.*(?:This|Here).*?\n/i, '');
  }

  // Remove multiple consecutive empty lines at the beginning or end
  result = result.replace(/^\n+/, '');
  result = result.replace(/\n+$/, '');

  return result;
}

/**
 * Cleans up excessive blank lines in code
 * Reduces multiple consecutive blank lines to a maximum of 2
 *
 * @param code - The code to clean
 * @returns Code with cleaned up blank lines
 */
function cleanupBlankLines(code: string): string {
  // Replace multiple consecutive blank lines with a maximum of 2
  return code.replace(/\n{3,}/g, '\n\n');
}
