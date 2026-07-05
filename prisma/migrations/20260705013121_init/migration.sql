-- CreateTable
CREATE TABLE "AdaptiveMemory" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "fieldLabelSemantic" TEXT NOT NULL,
    "userInput" TEXT NOT NULL,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);
