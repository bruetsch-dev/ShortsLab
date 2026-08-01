$ErrorActionPreference = 'Stop'

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sceneFile = Join-Path $projectDir 'scene_descriptions.tsv'
$outputFile = Join-Path $projectDir 'image_prompts.txt'
$comfyOutputFile = Join-Path $projectDir 'image_prompts_comfyui.txt'

$prefix = 'Hand-drawn 2D doodle cartoon animation, flat colors, bold black outlines, slightly imperfect sketchy marker lines, '
$suffix = ', no gradients, no shadows, no textures, no photorealism, no 3D, 16:9 aspect ratio, educational YouTube explainer doodle style.'

$prompts = foreach ($line in Get-Content -LiteralPath $sceneFile) {
    if ([string]::IsNullOrWhiteSpace($line)) {
        continue
    }

    $parts = $line -split "`t", 2
    if ($parts.Count -ne 2) {
        throw "Invalid scene line: $line"
    }

    "[$($parts[0])] $prefix$($parts[1])$suffix"
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($outputFile, ($prompts -join "`r`n"), $utf8NoBom)

$comfyPrompts = foreach ($line in Get-Content -LiteralPath $sceneFile) {
    if ([string]::IsNullOrWhiteSpace($line)) {
        continue
    }

    $parts = $line -split "`t", 2
    "$prefix$($parts[1])$suffix"
}

[System.IO.File]::WriteAllText($comfyOutputFile, ($comfyPrompts -join "`r`n"), $utf8NoBom)
