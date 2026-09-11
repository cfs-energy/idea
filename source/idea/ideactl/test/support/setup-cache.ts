/**
 * Cache setup that produces plain data and clone it for each test.
 *
 * Cloning keeps mutations in one assertion from changing another assertion's
 * input. Callers must use this only when the underlying fixture is immutable
 * for the lifetime of the test file.
 */
export function cacheSetup<Result>(setup: () => Promise<Result>): () => Promise<Result> {
  let pending: Promise<Result> | undefined;

  return async (): Promise<Result> => {
    pending ??= setup();
    return structuredClone(await pending);
  };
}
