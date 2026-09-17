/**
 * Terminal question types and prompt drivers used by installer flows.
 *
 * The driver boundary keeps question collection independent from a real terminal. Callers can
 * supply scripted answers in tests or use the readline-backed driver in the command line tool.
 */

import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";

/** One option displayed by a select or checkbox question. */
export interface InstallerChoice {
  title: string;
  value: string;
  disabled?: boolean;
}

/** Question shapes declared by the installer parameter YAML. */
export type InstallerPromptType = "text" | "select" | "checkbox" | "confirm";

/** A normalised parameter declaration ready for a terminal driver. */
export interface InstallerQuestion {
  name: string;
  title: string;
  description: string;
  promptType: InstallerPromptType;
  multiple: boolean;
  defaultValue?: unknown;
  choices: InstallerChoice[];
  helpText?: string;
}

/** Collects one answer and optionally displays validation feedback. */
export interface PromptDriver {
  ask(question: InstallerQuestion): Promise<unknown>;
  report(message: string): void;
}

/** Parses a comma-separated terminal selection as choice values or one-based choice indexes. */
function parseSelection(answer: string, choices: readonly InstallerChoice[], multiple: boolean): string | string[] {
  const tokens = multiple ? answer.split(",") : [answer];
  const values = tokens
    .map((token) => token.trim())
    .filter((token) => token !== "")
    .map((token) => {
      const index = Number.parseInt(token, 10);
      if (/^\d+$/.test(token) && index >= 1 && index <= choices.length) {
        return choices[index - 1]?.value ?? token;
      }
      return token;
    });
  return multiple ? values : (values[0] ?? "");
}

/** Builds a compact terminal suffix without putting choice labels into the answer format. */
function suffix(question: InstallerQuestion): string {
  if (question.promptType === "confirm") {
    return question.defaultValue === false ? " [y/N] " : " [Y/n] ";
  }
  if (question.choices.length > 0) return question.multiple ? " [comma-separated values] " : " [value] ";
  return question.defaultValue === undefined ? " " : ` [${String(question.defaultValue)}] `;
}

/**
 * Uses stdin/stdout directly. Answers are intentionally returned untyped because validation and
 * type conversion are owned by the parameter runner.
 */
export class TerminalPromptDriver implements PromptDriver {
  async ask(question: InstallerQuestion): Promise<unknown> {
    if (question.description !== "") stdout.write(`${question.description}\n`);
    if (question.helpText !== undefined && question.helpText !== "") stdout.write(`${question.helpText}\n`);
    if (question.choices.length > 0) {
      for (const [index, choice] of question.choices.entries()) {
        const unavailable = choice.disabled === true ? " (unavailable)" : "";
        stdout.write(`${index + 1}. ${choice.title === "" ? choice.value : choice.title}${unavailable}\n`);
      }
    }

    const reader = createInterface({ input: stdin, output: stdout });
    try {
      const answer = (await reader.question(`${question.title}${suffix(question)}`)).trim();
      if (answer === "") return question.defaultValue;
      if (question.promptType === "confirm") return answer;
      if (question.promptType === "select" || question.promptType === "checkbox") {
        return parseSelection(answer, question.choices, question.multiple);
      }
      return answer;
    } finally {
      reader.close();
    }
  }

  report(message: string): void {
    stdout.write(`${message}\n`);
  }
}

/**
 * Test-only answer source. An array supplies successive retry answers, while a scalar supplies
 * the answer once. An absent answer lets the runner apply the declared default.
 */
export class ScriptedPromptDriver implements PromptDriver {
  readonly answers: Map<string, unknown[]>;
  readonly messages: string[] = [];

  constructor(answers: Record<string, unknown | unknown[]>) {
    this.answers = new Map(
      Object.entries(answers).map(([name, value]) => [name, Array.isArray(value) ? [...value] : [value]]),
    );
  }

  async ask(question: InstallerQuestion): Promise<unknown> {
    const answers = this.answers.get(question.name);
    if (answers === undefined || answers.length === 0) return question.defaultValue;
    return answers.shift();
  }

  report(message: string): void {
    this.messages.push(message);
  }
}
