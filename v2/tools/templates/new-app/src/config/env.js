// config -- the one place this app reads/validates environment
// variables (Vite only exposes import.meta.env entries prefixed
// VITE_ to client code -- see https://vite.dev/guide/env-and-mode).
// Nothing outside this file reads import.meta.env directly.
import { z } from "zod";

const EnvSchema = z.object({
  VITE_APP_NAME: z.string().default("__APP__"),
});

export const env = EnvSchema.parse(import.meta.env);
