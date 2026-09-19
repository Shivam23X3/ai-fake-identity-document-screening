// Hardhat config for the local audit-anchor chain (Step 9).
// Network "localhost": `npx hardhat node` (or `npx hardhat run --network localhost`).
require("@nomicfoundation/hardhat-toolbox");

// RPC override for Docker: inside a compose network the node is reachable as
// http://hardhat:8545, not 127.0.0.1. Unset CHAIN_RPC_URL keeps local dev
// behavior identical.
const LOCALHOST_URL = process.env.CHAIN_RPC_URL || "http://127.0.0.1:8545";

/** @type import('hardhat/config').HardhatUserConfig */
module.exports = {
  solidity: "0.8.24",
  networks: {
    hardhat: {
      chainId: 31337,
    },
    localhost: {
      url: LOCALHOST_URL,
      chainId: 31337,
    },
  },
};
