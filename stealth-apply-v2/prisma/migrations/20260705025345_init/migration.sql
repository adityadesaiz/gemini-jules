-- CreateTable
CREATE TABLE "AdaptiveFormMemory" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "fieldLabelSemantic" TEXT NOT NULL,
    "userResponse" TEXT NOT NULL,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updatedAt" DATETIME NOT NULL
);

-- CreateTable
CREATE TABLE "AppFunnelEntry" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "fieldCharacteristics" JSONB NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'stuck',
    "triggeredAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- CreateIndex
CREATE UNIQUE INDEX "AdaptiveFormMemory_fieldLabelSemantic_key" ON "AdaptiveFormMemory"("fieldLabelSemantic");
