$path = 'C:\Users\Roy\.openclaw\workspace-trading\runtime\trading_state.json'
$content = Get-Content -Path $path -Raw -Encoding UTF8
if ($content.StartsWith([char]0xFEFF)) {
    $content = $content.Substring(1)
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($path, $content, $utf8NoBom)
Write-Host "BOM removed successfully"
