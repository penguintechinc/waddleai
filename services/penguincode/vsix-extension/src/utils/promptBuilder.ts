/**
 * Prompt Builder Utility
 * Constructs focused prompts for different code assistance tasks
 */

/**
 * Map of language IDs to display names
 */
export const LANGUAGE_NAMES: Record<string, string> = {
  python: 'Python',
  javascript: 'JavaScript',
  typescript: 'TypeScript',
  jsx: 'JSX',
  tsx: 'TSX',
  java: 'Java',
  csharp: 'C#',
  cpp: 'C++',
  c: 'C',
  rust: 'Rust',
  go: 'Go',
  ruby: 'Ruby',
  php: 'PHP',
  swift: 'Swift',
  kotlin: 'Kotlin',
  scala: 'Scala',
  r: 'R',
  sql: 'SQL',
  html: 'HTML',
  css: 'CSS',
  json: 'JSON',
  yaml: 'YAML',
  xml: 'XML',
  markdown: 'Markdown',
  shell: 'Shell',
  bash: 'Bash',
  powershell: 'PowerShell',
  dockerfile: 'Dockerfile',
  lua: 'Lua',
  perl: 'Perl',
};

/**
 * Get the display name for a language ID
 * @param languageId - The language ID (e.g., 'typescript')
 * @returns The display name or the original ID if not found
 */
function getLanguageName(languageId: string): string {
  return LANGUAGE_NAMES[languageId.toLowerCase()] || languageId;
}

/**
 * Build a prompt for inline code completion
 * Completes code based on prefix and suffix context
 *
 * @param prefix - The code before the cursor
 * @param suffix - The code after the cursor
 * @param language - The programming language ID
 * @returns A focused completion prompt
 */
export function buildCompletionPrompt(
  prefix: string,
  suffix: string,
  language: string
): string {
  const languageName = getLanguageName(language);

  return `You are an expert ${languageName} code completion assistant. Complete the code at the cursor position.

Context:
Before cursor:
\`\`\`${language}
${prefix}
\`\`\`

After cursor:
\`\`\`${language}
${suffix}
\`\`\`

Complete the code at the cursor. Return only the completion text without any explanation or markdown formatting. Match the existing code style and indentation.`;
}

/**
 * Build a prompt for code generation from comments
 * Generates code implementation based on a description or comment
 *
 * @param context - The surrounding code context or comment describing what to generate
 * @param language - The programming language ID
 * @returns A focused generation prompt
 */
export function buildGenerationPrompt(context: string, language: string): string {
  const languageName = getLanguageName(language);

  return `You are an expert ${languageName} code generator. Generate clean, well-structured code based on the provided context.

Context:
\`\`\`${language}
${context}
\`\`\`

Generate the appropriate ${languageName} code to fulfill the requirements. Return only the generated code without any explanation or markdown formatting. Follow best practices and conventions for ${languageName}.`;
}

/**
 * Build a prompt for explaining code
 * Provides a clear explanation of what the given code does
 *
 * @param code - The code to explain
 * @param language - The programming language ID
 * @returns A focused explanation prompt
 */
export function buildExplanationPrompt(code: string, language: string): string {
  const languageName = getLanguageName(language);

  return `You are an expert ${languageName} code documentation specialist. Explain the following code clearly and concisely.

Code:
\`\`\`${language}
${code}
\`\`\`

Provide a clear, concise explanation of what this code does, including:
1. The main purpose and functionality
2. Key operations or logic
3. Any important side effects or dependencies
4. Input and output behavior (if applicable)

Be direct and avoid unnecessary verbosity.`;
}

/**
 * Build a prompt for refactoring code
 * Suggests improvements to code quality, readability, and performance
 *
 * @param code - The code to refactor
 * @param language - The programming language ID
 * @returns A focused refactoring prompt
 */
export function buildRefactorPrompt(code: string, language: string): string {
  const languageName = getLanguageName(language);

  return `You are an expert ${languageName} code refactoring specialist. Improve the following code for better readability, maintainability, and performance.

Code:
\`\`\`${language}
${code}
\`\`\`

Refactor this ${languageName} code by:
1. Improving code clarity and readability
2. Following ${languageName} best practices and conventions
3. Optimizing for performance if applicable
4. Reducing complexity or duplication

Return the refactored code without any explanation. Maintain the same functionality while improving code quality.`;
}

/**
 * Build a prompt for fixing code issues
 * Fixes bugs, errors, or issues in the provided code
 *
 * @param code - The code to fix
 * @param language - The programming language ID
 * @param error - Optional error message or description of the issue
 * @returns A focused fix prompt
 */
export function buildFixPrompt(
  code: string,
  language: string,
  error?: string
): string {
  const languageName = getLanguageName(language);

  let prompt = `You are an expert ${languageName} code debugger. Fix the issues in the following code.

Code:
\`\`\`${language}
${code}
\`\`\``;

  if (error) {
    prompt += `\n\nError or Issue:\n${error}`;
  }

  prompt += `\n\nFix the code to resolve the issue. Return only the corrected code without any explanation. Maintain the original intent and structure while fixing the problem.`;

  return prompt;
}
