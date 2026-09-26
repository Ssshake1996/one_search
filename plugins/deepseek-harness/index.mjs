import * as mcpClient from '@deepseek-ai/dsh-mcp-client';
import { installedCommand, prepareService, settings } from './bootstrap.mjs';
import { registerWebHost } from './web-host.mjs';
import { coordinatorOptions, createUpgradeCoordinator } from './upgrade-coordinator.mjs';

export const name = 'one-search';
export const inject = ['tools'];

// Explicitly async: Cordis awaits startup work before making tools available.
export async function apply(ctx, config = {}) {
  const options = await coordinatorOptions(settings(config));
  options.command ||= await installedCommand(options);
  const coordinator = await createUpgradeCoordinator(options, { onError(error) {
    ctx.logger.error(`one_search MCP startup/resume failed: ${error.code}` +
      (error.operation ? ` (${error.operation}: ${error.backend_code || 'failed'})` : '') +
      '; inspect backend availability, then reload this profile to retry');
  } });
  ctx.effect(() => () => coordinator.dispose(), 'one-search: upgrade coordination');
  let connection;
  registerWebHost(ctx, () => connection, { runRuntime: coordinator.runRuntime, maintenanceStatus: coordinator.status });
  coordinator.setResumeHandler(async () => {
    connection = await prepareService(config, undefined, { runRuntime: coordinator.runRuntime });
    await coordinator.runRuntime(async () => {
      const fiber = ctx.plugin(mcpClient, connection);
      coordinator.setMcpFiber(fiber);
      await fiber.await();
    });
  });
  await coordinator.start();
}
