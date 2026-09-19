const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("AuditAnchor", function () {
  async function deploy() {
    const factory = await ethers.getContractFactory("AuditAnchor");
    const contract = await factory.deploy();
    await contract.waitForDeployment();
    return contract;
  }

  const RUN_ID = 123456789;
  const REPORT_HASH = ethers.id("report-hash-canonical-json");
  const REVIEWER_HASH = ethers.id("reviewer-decision");
  const LINKAGE = ethers.id("AuditAnchor");

  it("anchors a report commitment and exposes it for verification", async function () {
    const c = await deploy();
    await c.anchor(RUN_ID, REPORT_HASH, REVIEWER_HASH, LINKAGE);

    const [reportHash, reviewerHash, ts, linkage] = await c.getAnchor(RUN_ID, REPORT_HASH);
    expect(reportHash).to.equal(REPORT_HASH);
    expect(reviewerHash).to.equal(REVIEWER_HASH);
    expect(ts).to.be.greaterThan(0);
    expect(linkage).to.equal(LINKAGE);
    expect(await c.isAnchored(RUN_ID, REPORT_HASH)).to.equal(true);
    expect(await c.anchorCount()).to.equal(1);
  });

  it("reverts on duplicate anchoring of the same commitment", async function () {
    const c = await deploy();
    await c.anchor(RUN_ID, REPORT_HASH, REVIEWER_HASH, LINKAGE);
    await expect(
      c.anchor(RUN_ID, REPORT_HASH, REVIEWER_HASH, LINKAGE)
    ).to.be.revertedWithCustomError(c, "AlreadyAnchored");
  });

  it("keeps distinct commitments independent (same id, different report)", async function () {
    const c = await deploy();
    const h2 = ethers.id("report-v2");
    await c.anchor(RUN_ID, REPORT_HASH, REVIEWER_HASH, LINKAGE);
    await c.anchor(RUN_ID, h2, REVIEWER_HASH, LINKAGE);
    expect(await c.anchorCount()).to.equal(2);
    expect(await c.isAnchored(RUN_ID, h2)).to.equal(true);
  });

  it("returns zeros for unknown commitments", async function () {
    const c = await deploy();
    const [reportHash, , ts] = await c.getAnchor(RUN_ID, REPORT_HASH);
    expect(reportHash).to.equal(ethers.ZeroHash);
    expect(ts).to.equal(0);
  });
});
