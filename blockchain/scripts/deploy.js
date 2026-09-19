// Deploy AuditAnchor and write blockchain/deployed.json + abi/AuditAnchor.json
// (the Python web3 client reads exactly these two files).
const fs = require("fs");
const path = require("path");
const hre = require("hardhat");

async function main() {
  const [deployer] = await hre.ethers.getSigners();
  console.log("Deploying AuditAnchor with account:", deployer.address);

  const factory = await hre.ethers.getContractFactory("AuditAnchor");
  const contract = await factory.deploy();
  await contract.waitForDeployment();

  const address = await contract.getAddress();
  console.log("AuditAnchor deployed to:", address);

  const base = __dirname ? path.resolve(__dirname, "..") : path.resolve("blockchain");

  fs.writeFileSync(
    path.join(base, "deployed.json"),
    JSON.stringify(
      {
        network: hre.network.name,
        chainId: Number((await hre.ethers.provider.getNetwork()).chainId),
        address,
        deployer: deployer.address,
        deployed_at: new Date().toISOString(),
      },
      null,
      2
    )
  );

  const abiDir = path.join(base, "abi");
  fs.mkdirSync(abiDir, { recursive: true });
  fs.writeFileSync(
    path.join(abiDir, "AuditAnchor.json"),
    JSON.stringify(factory.interface.formatJson(), null, 2)
  );

  console.log("Wrote deployed.json + abi/AuditAnchor.json");
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
