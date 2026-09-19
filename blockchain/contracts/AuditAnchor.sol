// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * @title AuditAnchor
 * @notice Tamper-evident audit anchoring for the document-screening system.
 *         Stores ONLY commitments: a 32-byte report hash, a 32-byte reviewer
 *         decision hash, a numeric screening id (first 32 bits of the run id)
 *         and a linkage proof. NO PII ever goes on chain — the full report
 *         lives in the local database and the chain merely proves it existed
 *         at a point in time, unchanged.
 */
contract AuditAnchor {
    struct Anchor {
        bytes32 reportHash;        // sha256(canonical report JSON), left-padded
        bytes32 reviewerHash;      // commitment of the human decision (0 until reviewed)
        uint64  timestamp;         // block time of anchoring
        bytes32 linkageCode;      // keccak256("AuditAnchor") — linkage proof
        bool    exists;
    }

    // numericScreeningId => reportHash => Anchor
    mapping(uint256 => mapping(bytes32 => Anchor)) private _anchors;
    uint256 public anchorCount;

    event Anchored(
        uint256 indexed numericScreeningId,
        bytes32 indexed reportHash,
        bytes32 reviewerHash,
        uint64 timestamp,
        bytes32 linkageCode
    );

    error AlreadyAnchored();

    /**
     * @notice Anchor one report commitment. Reverts when the exact
     *         (screeningId, reportHash) pair was already anchored — callers
     *         treat that as idempotent success.
     */
    function anchor(
        uint256 numericScreeningId,
        bytes32 reportHash,
        bytes32 reviewerHash,
        bytes32 linkageCode
    ) external {
        if (_anchors[numericScreeningId][reportHash].exists) {
            revert AlreadyAnchored();
        }
        _anchors[numericScreeningId][reportHash] = Anchor({
            reportHash: reportHash,
            reviewerHash: reviewerHash,
            timestamp: uint64(block.timestamp),
            linkageCode: linkageCode,
            exists: true
        });
        anchorCount += 1;
        emit Anchored(numericScreeningId, reportHash, reviewerHash, uint64(block.timestamp), linkageCode);
    }

    /**
     * @notice Read one anchor. Returns (reportHash, reviewerHash, timestamp,
     *         linkageCode) — zero hash + timestamp 0 when absent.
     */
    function getAnchor(
        uint256 numericScreeningId,
        bytes32 reportHash
    ) external view returns (bytes32, bytes32, uint64, bytes32) {
        Anchor storage a = _anchors[numericScreeningId][reportHash];
        return (a.reportHash, a.reviewerHash, a.timestamp, a.linkageCode);
    }

    /// @notice True when this exact commitment exists on chain.
    function isAnchored(uint256 numericScreeningId, bytes32 reportHash) external view returns (bool) {
        return _anchors[numericScreeningId][reportHash].exists;
    }
}
