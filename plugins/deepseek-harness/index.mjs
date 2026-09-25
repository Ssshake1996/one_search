import * as mcpClient from '@deepseek-ai/dsh-mcp-client';
import { prepareService } from './bootstrap.mjs';

export const name = 'one-search';
export const inject = ['tools'];

// Explicitly async: Cordis awaits startup work before making tools available.
export async function apply(ctx, config = {}) {
  const connection = await prepareService(config);
  await mcpClient.apply(ctx, connection);
}
