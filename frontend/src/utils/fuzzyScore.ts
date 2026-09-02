// Fuzzy matcher ported from Vercel cmdk's command-score (MIT, (c) Paco Coursey /
// cmdk contributors — https://github.com/dip/cmdk, cmdk/src/command-score.ts).
// Absorbed per the cmdk review decision: keep our hand-rolled mention menu, adopt its
// scoring + a11y model instead of installing the package (the menu is caret-triggered
// inline completion over ≤8 attachments — cmdk's 2-3k-item combobox model does not fit).
//
// Scoring contract: a continuous exact match scores 1; word-boundary jumps beat character
// jumps; transpositions and case mismatches are penalized; 0 means "no match".

const SCORE_CONTINUE_MATCH = 1;
const SCORE_SPACE_WORD_JUMP = 0.9;
const SCORE_NON_SPACE_WORD_JUMP = 0.8;
const SCORE_CHARACTER_JUMP = 0.17;
const SCORE_TRANSPOSITION = 0.1;
const PENALTY_SKIPPED = 0.999;
const PENALTY_CASE_MISMATCH = 0.9999;
// PENALTY_DISTANCE_FROM_START is part of cmdk's published scoring contract but unused by
// the exported paths (kept out to satisfy noUnusedLocals; behavior identical to upstream).
// const PENALTY_DISTANCE_FROM_START = 0.9;
const PENALTY_NOT_COMPLETE = 0.99;

const IS_GAP_REGEXP = /[\\\/_+.#"@\[\(\{&]/;
const COUNT_GAPS_REGEXP = /[\\\/_+.#"@\[\(\{&]/g;
const IS_SPACE_REGEXP = /[\s-]/;
const COUNT_SPACE_REGEXP = /[\s-]/g;

function commandScoreInner(
  string: string,
  abbreviation: string,
  lowerString: string,
  lowerAbbreviation: string,
  stringIndex: number,
  abbreviationIndex: number,
  memoizedResults: Record<string, number>
): number {
  if (abbreviationIndex === abbreviation.length) {
    if (stringIndex === string.length) {
      return SCORE_CONTINUE_MATCH;
    }
    return PENALTY_NOT_COMPLETE;
  }

  const memoizeKey = `${stringIndex},${abbreviationIndex}`;
  if (memoizedResults[memoizeKey] !== undefined) {
    return memoizedResults[memoizeKey];
  }

  const abbreviationChar = lowerAbbreviation.charAt(abbreviationIndex);
  let index = lowerString.indexOf(abbreviationChar, stringIndex);
  let highScore = 0;

  let score: number;
  let transposedScore: number;
  let wordBreaks: RegExpMatchArray | null;
  let spaceBreaks: RegExpMatchArray | null;

  while (index >= 0) {
    score = commandScoreInner(
      string,
      abbreviation,
      lowerString,
      lowerAbbreviation,
      index + 1,
      abbreviationIndex + 1,
      memoizedResults
    );
    if (score > highScore) {
      if (index === stringIndex) {
        score *= SCORE_CONTINUE_MATCH;
      } else if (IS_GAP_REGEXP.test(string.charAt(index - 1))) {
        score *= SCORE_NON_SPACE_WORD_JUMP;
        wordBreaks = string.slice(stringIndex, index - 1).match(COUNT_GAPS_REGEXP);
        if (wordBreaks && stringIndex > 0) {
          score *= Math.pow(PENALTY_SKIPPED, wordBreaks.length);
        }
      } else if (IS_SPACE_REGEXP.test(string.charAt(index - 1))) {
        score *= SCORE_SPACE_WORD_JUMP;
        spaceBreaks = string.slice(stringIndex, index - 1).match(COUNT_SPACE_REGEXP);
        if (spaceBreaks && stringIndex > 0) {
          score *= Math.pow(PENALTY_SKIPPED, spaceBreaks.length);
        }
      } else {
        score *= SCORE_CHARACTER_JUMP;
        if (stringIndex > 0) {
          score *= Math.pow(PENALTY_SKIPPED, index - stringIndex);
        }
      }

      if (string.charAt(index) !== abbreviation.charAt(abbreviationIndex)) {
        score *= PENALTY_CASE_MISMATCH;
      }
    }

    if (
      (score < SCORE_TRANSPOSITION &&
        stringIndex > 0 &&
        lowerString.charAt(index - 1) === lowerAbbreviation.charAt(abbreviationIndex + 1)) ||
      (lowerAbbreviation.charAt(abbreviationIndex + 1) === lowerAbbreviation.charAt(abbreviationIndex) &&
        stringIndex > 0 &&
        lowerString.charAt(index - 1) !== lowerAbbreviation.charAt(abbreviationIndex))
    ) {
      transposedScore = commandScoreInner(
        string,
        abbreviation,
        lowerString,
        lowerAbbreviation,
        index + 1,
        abbreviationIndex + 2,
        memoizedResults
      );
      if (transposedScore * SCORE_TRANSPOSITION > score) {
        score = transposedScore * SCORE_TRANSPOSITION;
      }
    }

    if (score > highScore) {
      highScore = score;
    }

    index = lowerString.indexOf(abbreviationChar, index + 1);
  }

  memoizedResults[memoizeKey] = highScore;
  return highScore;
}

function formatInput(string: string): string {
  // Normalize all valid space characters to plain spaces so they match each other.
  return string.toLowerCase().replace(COUNT_SPACE_REGEXP, ' ');
}

export function fuzzyScore(string: string, abbreviation: string, aliases: string[] = []): number {
  if (!abbreviation) {
    return 0;
  }
  if (aliases && aliases.length > 0) {
    string = `${string} ${aliases.join(' ')}`;
  }
  return commandScoreInner(string, abbreviation, formatInput(string), formatInput(abbreviation), 0, 0, {});
}

// Rank a candidate list by fuzzy score: non-matches (score 0) drop out, the rest sort by
// score descending with a stable tie-break on the original order (cmdk keeps DOM order
// for equal scores — the consumer's render order wins).
export function fuzzyRank<T>(items: T[], query: string, keyOf: (item: T) => string): T[] {
  return items
    .map((item, index) => ({ item, index, score: fuzzyScore(keyOf(item), query) }))
    .filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .map((entry) => entry.item);
}
