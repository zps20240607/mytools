# 每日清单.ps1 - Daily Todo Widget (native WPF, gray glass theme)
# Features: QQ-style top-dock auto hide/show, draggable, gray translucent card,
# add / toggle / delete / double-click edit tasks, clear completed, JSON persistence.

Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName WindowsBase

$DataFile = Join-Path $PSScriptRoot '每日清单数据.json'

# ============================================================
# XAML UI (gray glass theme)
# ============================================================
$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="每日清单" Width="340" Height="560"
        WindowStyle="None" AllowsTransparency="True" Background="Transparent"
        Topmost="False" ShowInTaskbar="False"
        FontFamily="Noto Serif SC, Source Han Serif SC, STSong, SimSun" FontSize="13.5">
  <Window.Resources>
    <!-- Flat icon button -->
    <Style x:Key="IconBtn" TargetType="Button">
      <Setter Property="Background" Value="Transparent"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="Foreground" Value="#5D5343"/>
      <Setter Property="FontSize" Value="14"/>
      <Setter Property="Padding" Value="6,3"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}" CornerRadius="5"
                    Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter Property="Background" Value="#142B2419"/>
                <Setter Property="Foreground" Value="#2B2419"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <!-- Add task button -->
    <Style x:Key="AddBtn" TargetType="Button">
      <Setter Property="Background" Value="#A33B2C"/>
      <Setter Property="Foreground" Value="#F4ECDC"/>
      <Setter Property="FontSize" Value="17"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="bd" Background="{TemplateBinding Background}" CornerRadius="5">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter Property="Background" Value="#8A2F22"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter Property="Background" Value="#75281D"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <!-- Round input box -->
    <Style x:Key="RoundBox" TargetType="TextBox">
      <Setter Property="Background" Value="#E6EDE3CF"/>
      <Setter Property="BorderBrush" Value="#2E2B2419"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="10,0"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="TextBox">
            <Border x:Name="bd" Background="{TemplateBinding Background}"
                    BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}" CornerRadius="5">
              <ScrollViewer x:Name="PART_ContentHost" Margin="{TemplateBinding Padding}" VerticalAlignment="Center"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsKeyboardFocused" Value="True">
                <Setter Property="BorderBrush" Value="#A33B2C"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <!-- Round checkbox -->
    <Style x:Key="CircleCheck" TargetType="CheckBox">
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="CheckBox">
            <Border x:Name="bd" Width="18" Height="18" CornerRadius="9"
                    BorderBrush="#5D5343" BorderThickness="2" Background="Transparent">
              <TextBlock x:Name="tick" Text="✓" Foreground="#F4ECDC" FontSize="11"
                         FontWeight="Bold" HorizontalAlignment="Center" VerticalAlignment="Center"
                         Visibility="Collapsed" Margin="0,-1,0,0"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsChecked" Value="True">
                <Setter Property="Background" Value="#A33B2C"/>
                <Setter Property="BorderBrush" Value="#A33B2C"/>
                <Setter TargetName="tick" Property="Visibility" Value="Visible"/>
              </Trigger>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter Property="BorderBrush" Value="#A33B2C"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
  </Window.Resources>

  <Border CornerRadius="6" Background="#E0F4ECDC" BorderBrush="#2E2B2419" BorderThickness="1">
    <Border.Effect>
      <DropShadowEffect BlurRadius="24" ShadowDepth="4" Opacity="0.25" Color="#000000"/>
    </Border.Effect>
    <Grid Margin="0,4,0,0">
      <Grid.RowDefinitions>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="Auto"/>
        <RowDefinition Height="*"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>

      <!-- Title bar (drag area) -->
      <Grid x:Name="TitleBar" Grid.Row="0" Margin="12,4,6,0" Background="Transparent" Cursor="Hand">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="Auto"/>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
          <ColumnDefinition Width="Auto"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <Border Width="22" Height="22" BorderBrush="#A33B2C" BorderThickness="1.5" CornerRadius="3"
                VerticalAlignment="Center" Margin="4,0,8,0">
          <TextBlock Text="单" Foreground="#A33B2C" FontSize="12" FontFamily="STKaiti, KaiTi, STSong"
                     HorizontalAlignment="Center" VerticalAlignment="Center"/>
        </Border>
        <TextBlock Grid.Column="1" Text="每日清单" FontWeight="SemiBold" FontSize="15"
                   FontFamily="STKaiti, KaiTi, STSong"
                   Foreground="#2B2419" VerticalAlignment="Center" Padding="0,4"/>
        <Button x:Name="BtnPin" Grid.Column="2" Style="{StaticResource IconBtn}" Content="📌"
                FontSize="12" ToolTip="固定在屏幕顶部（不自动收起）"/>
        <Button x:Name="BtnReset" Grid.Column="3" Style="{StaticResource IconBtn}" Content="⟳" ToolTip="复位到屏幕顶部"/>
        <Button x:Name="BtnClose" Grid.Column="4" Style="{StaticResource IconBtn}" Content="✕" ToolTip="关闭"/>
      </Grid>

      <!-- Date line -->
      <TextBlock x:Name="DateLine" Grid.Row="1" Margin="20,6,18,4" FontSize="12.5" Foreground="#5D5343"/>

      <!-- Input row -->
      <Grid Grid.Row="2" Margin="16,4,16,10">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <TextBox x:Name="Input" Style="{StaticResource RoundBox}" Height="34" FontSize="13.5"
                 Foreground="#3A3A3A" Margin="0,0,8,0" ToolTip="添加新任务，回车提交，Esc 清空"/>
        <Button x:Name="BtnAdd" Grid.Column="1" Style="{StaticResource AddBtn}"
                Content="＋" Width="36" Height="34" ToolTip="添加任务"/>
      </Grid>

      <!-- Task list -->
      <Grid Grid.Row="3" Margin="6,0,6,0">
        <ScrollViewer VerticalScrollBarVisibility="Auto">
          <ItemsControl x:Name="TodoList"/>
        </ScrollViewer>
        <TextBlock x:Name="EmptyHint" Text="今天没有待办，休息一下吧&#x0A;（双击任务可编辑）" Foreground="#9A9A9A" FontSize="13"
                   TextAlignment="Center" HorizontalAlignment="Center" VerticalAlignment="Center" Visibility="Collapsed"/>
      </Grid>

      <!-- Footer -->
      <Border Grid.Row="4" BorderThickness="0,1,0,0" BorderBrush="#DCDCDC" Margin="0,6,0,0" Padding="18,6,10,8">
        <Grid>
          <TextBlock x:Name="CountText" Text="剩余 0 项" FontSize="12.5" Foreground="#8C8C8C" VerticalAlignment="Center"/>
          <Button x:Name="BtnClearDone" HorizontalAlignment="Right" Style="{StaticResource IconBtn}"
                  Content="清除已完成" FontSize="12.5"/>
        </Grid>
      </Border>
    </Grid>
  </Border>
</Window>
'@

$reader = New-Object System.Xml.XmlNodeReader ([xml]$xaml)
$win = [Windows.Markup.XamlReader]::Load($reader)

$TitleBar     = $win.FindName('TitleBar')
$BtnPin       = $win.FindName('BtnPin')
$BtnReset     = $win.FindName('BtnReset')
$BtnClose     = $win.FindName('BtnClose')
$DateLine     = $win.FindName('DateLine')
$InputBox     = $win.FindName('Input')
$BtnAdd       = $win.FindName('BtnAdd')
$TodoList     = $win.FindName('TodoList')
$EmptyHint    = $win.FindName('EmptyHint')
$CountText    = $win.FindName('CountText')
$BtnClearDone = $win.FindName('BtnClearDone')

$brushConverter = New-Object System.Windows.Media.BrushConverter
$brushMain  = $brushConverter.ConvertFrom('#3A3A3A')
$brushSub   = $brushConverter.ConvertFrom('#8C8C8C')
$brushHover = $brushConverter.ConvertFrom('#E6E6E6')

# ============================================================
# Data (JSON persistence)
# Model: { id; text; completed; createdAt }
# ============================================================
$script:todos = New-Object System.Collections.ArrayList

function Load-Todos {
    if (-not (Test-Path $DataFile)) { return }
    try {
        $raw = Get-Content $DataFile -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($raw)) { return }
        foreach ($t in @(ConvertFrom-Json $raw)) {
            if ($null -ne $t -and $null -ne $t.text) { [void]$script:todos.Add($t) }
        }
    } catch {}
}

function Save-Todos {
    try {
        if ($script:todos.Count -eq 0) {
            Set-Content -Path $DataFile -Value '[]' -Encoding UTF8
        } else {
            $json = ConvertTo-Json -InputObject @($script:todos) -Depth 3
            Set-Content -Path $DataFile -Value $json -Encoding UTF8
        }
    } catch {}
}

# ============================================================
# Rendering
# ============================================================
function Set-RowVisual($tb, [bool]$done) {
    if ($done) {
        $tb.TextDecorations = [System.Windows.TextDecorations]::Strikethrough
        $tb.Foreground = $brushSub
    } else {
        $tb.TextDecorations = $null
        $tb.Foreground = $brushMain
    }
}

function Update-Count {
    $done = @($script:todos | Where-Object { $_.completed }).Count
    $left = $script:todos.Count - $done
    if ($done -gt 0) {
        $CountText.Text = "剩余 $left 项 · 已完成 $done 项"
    } else {
        $CountText.Text = "剩余 $left 项"
    }
    $EmptyHint.Visibility = $(if ($script:todos.Count -eq 0) { 'Visible' } else { 'Collapsed' })
}

# Double-click a task text to edit it in place
function Start-Edit($tb) {
    $todo = $tb.Tag
    if ($null -eq $todo) { return }
    $g = $tb.Parent
    if ($null -eq $g) { return }

    $eb = New-Object System.Windows.Controls.TextBox
    $eb.Style = $win.FindResource('RoundBox')
    $eb.Text = [string]$todo.text
    $eb.FontSize = 13
    $eb.Height = 28
    $eb.Margin = New-Object System.Windows.Thickness(0,2,4,2)
    $eb.Tag = @{ Todo = $todo; Done = $false }
    [System.Windows.Controls.Grid]::SetColumn($eb, 1)

    $commit = {
        param($s, $e)
        $info = $s.Tag
        if ($info.Done) { return }
        $info.Done = $true
        $newText = $s.Text.Trim()
        if ($newText) { $info.Todo.text = $newText }
        Render-List
        Save-Todos
    }
    $eb.Add_LostFocus($commit)
    $eb.Add_KeyDown({
        param($s, $e)
        if ($e.Key -eq 'Return') {
            $e.Handled = $true
            $info = $s.Tag
            if (-not $info.Done) {
                $info.Done = $true
                $newText = $s.Text.Trim()
                if ($newText) { $info.Todo.text = $newText }
                Render-List
                Save-Todos
            }
        }
        elseif ($e.Key -eq 'Escape') { $s.Tag.Done = $true; Render-List }
    })

    $idx = $g.Children.IndexOf($tb)
    if ($idx -ge 0) {
        $g.Children.RemoveAt($idx)
        [void]$g.Children.Insert($idx, $eb)
        [void]$eb.Focus()
        $eb.SelectAll()
    }
}

function New-TodoRow($todo) {
    $g = New-Object System.Windows.Controls.Grid
    $g.Margin = New-Object System.Windows.Thickness(4,1,4,1)
    $g.Background = [System.Windows.Media.Brushes]::Transparent
    $c1 = New-Object System.Windows.Controls.ColumnDefinition; $c1.Width = 'Auto'
    $c2 = New-Object System.Windows.Controls.ColumnDefinition
    $c3 = New-Object System.Windows.Controls.ColumnDefinition; $c3.Width = 'Auto'
    [void]$g.ColumnDefinitions.Add($c1)
    [void]$g.ColumnDefinitions.Add($c2)
    [void]$g.ColumnDefinitions.Add($c3)

    $cb = New-Object System.Windows.Controls.CheckBox
    $cb.Style = $win.FindResource('CircleCheck')
    $cb.VerticalAlignment = 'Center'
    $cb.Margin = New-Object System.Windows.Thickness(8,0,2,0)
    $cb.IsChecked = [bool]$todo.completed

    $tb = New-Object System.Windows.Controls.TextBlock
    $tb.Text = [string]$todo.text
    $tb.TextWrapping = 'Wrap'
    $tb.VerticalAlignment = 'Center'
    $tb.Margin = New-Object System.Windows.Thickness(4,7,4,7)
    $tb.FontSize = 13.5
    $tb.ToolTip = '双击编辑'
    $tb.Tag = $todo

    $del = New-Object System.Windows.Controls.Button
    $del.Content = '✕'
    $del.Style = $win.FindResource('IconBtn')
    $del.VerticalAlignment = 'Center'
    $del.ToolTip = '删除任务'

    Set-RowVisual $tb ([bool]$todo.completed)

    $g.Add_MouseEnter({ param($s,$e) $s.Background = $brushHover })
    $g.Add_MouseLeave({ param($s,$e) $s.Background = [System.Windows.Media.Brushes]::Transparent })

    $cb.Tag = @{ Todo = $todo; Tb = $tb }
    $del.Tag = $todo

    $cb.Add_Click({
        param($s, $e)
        $info = $s.Tag
        $info.Todo.completed = [bool]$s.IsChecked
        Set-RowVisual $info.Tb ([bool]$s.IsChecked)
        Save-Todos
        Update-Count
    })
    $del.Add_Click({
        param($s, $e)
        [void]$script:todos.Remove($s.Tag)
        Render-List
        Save-Todos
    })
    $tb.Add_MouseLeftButtonDown({
        param($s, $e)
        if ($e.ClickCount -eq 2) { Start-Edit $s; $e.Handled = $true }
    })

    [System.Windows.Controls.Grid]::SetColumn($cb, 0)
    [System.Windows.Controls.Grid]::SetColumn($tb, 1)
    [System.Windows.Controls.Grid]::SetColumn($del, 2)
    [void]$g.Children.Add($cb)
    [void]$g.Children.Add($tb)
    [void]$g.Children.Add($del)
    return $g
}

function Render-List {
    $TodoList.Items.Clear()
    foreach ($t in $script:todos) {
        [void]$TodoList.Items.Add((New-TodoRow $t))
    }
    Update-Count
}

# ============================================================
# Task actions
# ============================================================
function Add-Todo {
    $text = $InputBox.Text.Trim()
    if (-not $text) { return }
    $todo = [PSCustomObject]@{
        id        = [DateTimeOffset]::Now.ToUnixTimeMilliseconds().ToString()
        text      = $text
        completed = $false
        createdAt = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    }
    $script:todos.Insert(0, $todo)   # newest on top
    $InputBox.Text = ''
    Render-List
    Save-Todos
    [void]$InputBox.Focus()
}

$BtnAdd.Add_Click({ Add-Todo })
$InputBox.Add_KeyDown({
    param($s, $e)
    if ($e.Key -eq 'Return') { Add-Todo; $e.Handled = $true }
    elseif ($e.Key -eq 'Escape') { $InputBox.Text = ''; $e.Handled = $true }
})

$BtnClearDone.Add_Click({
    $doneCount = @($script:todos | Where-Object { $_.completed }).Count
    if ($doneCount -eq 0) {
        $BtnClearDone.Content = '没有已完成任务'
        $script:flashTimer.Stop()
        $script:flashTimer.Start()
        return
    }
    $keep = @($script:todos | Where-Object { -not $_.completed })
    $script:todos.Clear()
    foreach ($t in $keep) { [void]$script:todos.Add($t) }
    Render-List
    Save-Todos
})

$script:flashTimer = New-Object System.Windows.Threading.DispatcherTimer
$script:flashTimer.Interval = [TimeSpan]::FromMilliseconds(900)
$script:flashTimer.Add_Tick({
    $script:flashTimer.Stop()
    $BtnClearDone.Content = '清除已完成'
})

# ============================================================
# Date line + midnight refresh
# ============================================================
$Weekdays = @('星期日','星期一','星期二','星期三','星期四','星期五','星期六')

function Update-DateLine {
    $now = Get-Date
    $DateLine.Text = '{0}年{1}月{2}日 · {3}' -f $now.Year,
        $now.Month.ToString('00'), $now.Day.ToString('00'), $Weekdays[[int]$now.DayOfWeek]
}

$dateTimer = New-Object System.Windows.Threading.DispatcherTimer
$dateTimer.Interval = [TimeSpan]::FromSeconds(30)
$dateTimer.Add_Tick({ Update-DateLine })
$dateTimer.Start()

# ============================================================
# QQ-style top dock: auto hide / auto show
# ============================================================
$script:pinned = $false     # pin: disable auto hide
$script:hidden = $false     # currently collapsed to top edge
$StripHeight = 6            # pixels left visible when hidden

# Smooth slide animation on Window.Top
$script:slideTarget = 0.0
$slideTimer = New-Object System.Windows.Threading.DispatcherTimer
$slideTimer.Interval = [TimeSpan]::FromMilliseconds(12)
$slideTimer.Add_Tick({
    $cur = $win.Top
    $diff = $script:slideTarget - $cur
    if ([Math]::Abs($diff) -lt 1.5) {
        $win.Top = $script:slideTarget
        $slideTimer.Stop()
    } else {
        $win.Top = $cur + $diff * 0.35
    }
})
function Slide-To([double]$target) {
    $script:slideTarget = $target
    $slideTimer.Start()
}

function Hide-Dock {
    if ($script:pinned -or $script:hidden) { return }
    if ($win.Top -gt 2) { return }              # only when docked at top
    if ($win.IsKeyboardFocusWithin) { return }  # never yank away while typing
    $script:hidden = $true
    Slide-To(-($win.Height - $StripHeight))
}

function Show-Dock {
    if (-not $script:hidden) { return }
    $script:hidden = $false
    Slide-To(0)
}

# Delayed hide after mouse leaves
$hideTimer = New-Object System.Windows.Threading.DispatcherTimer
$hideTimer.Interval = [TimeSpan]::FromMilliseconds(400)
$hideTimer.Add_Tick({
    $hideTimer.Stop()
    if ([System.Windows.Input.Mouse]::LeftButton -eq [System.Windows.Input.MouseButtonState]::Pressed) { return }
    Hide-Dock
})

$win.Add_MouseEnter({
    $hideTimer.Stop()
    if ($script:hidden) { Show-Dock }
})
$win.Add_MouseLeave({
    if ($script:pinned -or $script:hidden) { return }
    if ($win.Top -le 2) { $hideTimer.Stop(); $hideTimer.Start() }
})

# ============================================================
# Drag + dock snap + reset + pin + close
# ============================================================
function Test-InButton($src) {
    try {
        while ($null -ne $src) {
            if ($src -is [System.Windows.Controls.Button]) { return $true }
            if ($src -is [System.Windows.Media.Visual] -or $src -is [System.Windows.Media.Media3D.Visual3D]) {
                $src = [System.Windows.Media.VisualTreeHelper]::GetParent($src)
            } elseif ($src -is [System.Windows.DependencyObject]) {
                $src = [System.Windows.LogicalTreeHelper]::GetParent($src)
            } else { break }
        }
    } catch {}
    return $false
}

$TitleBar.Add_MouseLeftButtonDown({
    param($s, $e)
    if (Test-InButton $e.OriginalSource) { return }
    # If currently hidden, expand first so dragging works normally
    if ($script:hidden) { Show-Dock }
    try { $win.DragMove() } catch {}
})

# Snap to top edge after dragging
$TitleBar.Add_MouseLeftButtonUp({
    if ($win.Top -gt -20 -and $win.Top -lt 24) {
        $script:hidden = $false
        Slide-To(0)
    }
})

function Set-DefaultPosition {
    $wa = [System.Windows.SystemParameters]::WorkArea
    $win.Left = $wa.Right - $win.Width - 40
    $win.Top  = 0
    $script:hidden = $false
}

$BtnReset.Add_Click({ Slide-To(0); $wa = [System.Windows.SystemParameters]::WorkArea; $win.Left = $wa.Right - $win.Width - 40; $script:hidden = $false })

$BtnPin.Add_Click({
    $script:pinned = -not $script:pinned
    if ($script:pinned) {
        $BtnPin.Foreground = $brushMain
        $BtnPin.ToolTip = '取消固定（鼠标移开自动收起）'
        if ($script:hidden) { Show-Dock }
    } else {
        $BtnPin.Foreground = $brushSub
        $BtnPin.ToolTip = '固定在屏幕顶部（不自动收起）'
    }
})

$BtnClose.Add_Click({ $win.Close() })
$win.Add_Closed({ Save-Todos })

# ============================================================
# Startup
# ============================================================
Load-Todos
Update-DateLine
Render-List

$win.Add_Loaded({
    Set-DefaultPosition
    [void]$InputBox.Focus()
})

[void]$win.ShowDialog()

# Make sure the whole process exits (no leftover console/port)
[Environment]::Exit(0)
