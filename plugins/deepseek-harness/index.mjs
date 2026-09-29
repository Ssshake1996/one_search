import * as mcpClient from '@deepseek-ai/dsh-mcp-client';
import { installedCommand, prepareService, settings } from './bootstrap.mjs';
import { registerWebHost, probeBackend } from './web-host.mjs';
import { coordinatorOptions, createUpgradeCoordinator } from './upgrade-coordinator.mjs';

export const name = 'one-search';
export const inject = ['tools'];

// Explicitly async: Cordis awaits startup work before making tools available.
export async function apply(ctx, config = {}) {
  const options = await coordinatorOptions(settings(config));
  options.command ||= await installedCommand(options);
  const coordinator = await createUpgradeCoordinator(options, { superviseService: true, pollMs: 1000, onError(error) {
    ctx.logger.error(`one_search MCP startup/resume failed: ${error.code}` +
      (error.operation ? ` (${error.operation}: ${error.backend_code || 'failed'})` : '') +
      '; inspect backend availability; unexpected failures retry with exponential backoff');
  } });
  ctx.effect(() => () => coordinator.dispose(), 'one-search: upgrade coordination');
  let connection = options.command ? { command: options.command,
    args: [...options.commandArgs, 'mcp', '--config', options.configPath] } : undefined;
  registerWebHost(ctx, () => connection, { runRuntime: coordinator.runRuntime,
    maintenanceStatus: coordinator.status, syncService: coordinator.syncService });
  coordinator.setProbeHandler(async () => {
    await probeBackend(connection);
    const prefix = 'mcp__' + options.serverName + '__';
    if (!ctx.tools.wireSchemas().schemas.some((tool) => tool.name.startsWith(prefix))) {
      throw new Error('MCP transport has no registered tools');
    }
  });
  coordinator.setResumeHandler(async () => {
    connection = await prepareService(config, undefined, { runRuntime: coordinator.runRuntime });
    await coordinator.runRuntime(async () => {
      const fiber = ctx.plugin(mcpClient, connection);
      coordinator.setMcpFiber(fiber);
      await fiber.await();
    });
  });
  // The management page must remain usable when the backend is offline/stopped.
  await coordinator.start().catch(() => {});
}
